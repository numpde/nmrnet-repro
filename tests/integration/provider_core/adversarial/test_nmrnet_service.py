"""Holds and interrupted setup never become another allocation or start."""

import asyncio
import threading
import unittest
from builtins import BaseExceptionGroup
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.fakes.api import response_headers
from tests.fakes.attempt_inventory import attempt
from tests.fakes.attempt_inventory import response as inventory_response
from tests.integration.nmrnet.service_ports import ServicePeers
from tests.integration.nmrnet.test_nmrnet_session import ScientificWorker

from nmrnet_provider.admission import AdmissionHeld
from nmrnet_provider.service import Announcement, ServiceFailed, ServiceRefused
from provider_core.api import ApiFailure
from provider_core.attempt_files import AttemptFiles
from provider_core.attempt_record import AcknowledgedAttempt, LocalExecutionPhase, TerminalPending
from provider_core.attempt_store import StoreRefused
from provider_core.controller import ControllerFailed
from provider_core.execution import ExecutionFailed
from provider_core.transport import ExchangeFailed


class ProviderServiceFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_retirement_failure_keeps_the_original_outage_and_stops_before_retry(self):
        async with ServicePeers() as fixture:
            service = fixture.service()
            outage = ApiFailure("read live inventory", "unavailable", cause=ExchangeFailed("Timed out", primary=TimeoutError()))
            cleanup = ExchangeFailed("Original read retirement remains unconfirmed", primary=OSError("PRIVATE_CLEANUP"))
            with (
                patch.object(service, "_admission", AsyncMock(side_effect=outage)),
                patch.object(fixture.api, "retire", AsyncMock(side_effect=cleanup)),
                self.assertRaises(ServiceFailed) as caught,
            ):
                await service.run(fixture.stop)
            causes = [cause for _operation, cause in caught.exception.failures]
            self.assertIn(outage, causes)
            self.assertIn(cleanup, causes)
            self.assertEqual(fixture.api_peer.requests, [])
            self.assertEqual(fixture.allocations, [])
            self.assertEqual(fixture.api_notices, [])
            await service.settle()

    async def test_authentication_or_explicit_wrong_environment_read_failure_never_retries(self):
        for status, topology in ((401, b"dev-local"), (503, b"web")):
            with self.subTest(status=status, topology=topology):
                async with ServicePeers() as fixture:
                    fixture.api_peer.response_status = status
                    fixture.api_peer.response_headers = response_headers(
                        status=status, topology=topology, request_id=b"request:refused"
                    )
                    service = fixture.service()
                    fixture.stack.push_async_callback(service.settle)
                    with self.assertRaises(ServiceFailed):
                        await asyncio.wait_for(service.run(fixture.stop), 1)
                    self.assertEqual(len(fixture.api_peer.requests), 1)
                    self.assertEqual(fixture.allocations, [])
                    self.assertEqual(fixture.api_notices, [])

    async def test_operational_science_failure_keeps_its_cause_without_publishing_an_input_failure(self):
        primary = OSError("PRIVATE_SCIENTIFIC_PROCESS_FAILURE")
        async with ServicePeers() as fixture:
            service = fixture.service()
            fixture.stack.push_async_callback(service.settle)
            with patch.object(ScientificWorker, "outcome", side_effect=primary), self.assertRaises(ServiceFailed) as caught:
                await asyncio.wait_for(service.run(fixture.stop), 3)
            work = [cause for operation, cause in caught.exception.failures if operation == "Attempt work"]
            self.assertEqual(len(work), 1)
            self.assertIsInstance(work[0], ControllerFailed)
            self.assertIsInstance(work[0].cause, ExecutionFailed)
            self.assertIs(work[0].cause.cause, primary)
            self.assertNotIn("PRIVATE_SCIENTIFIC_PROCESS_FAILURE", str(caught.exception))
            self.assertNotIn("PRIVATE_SCIENTIFIC_PROCESS_FAILURE", str(work[0]))
            self.assertTrue(fixture.allocations[0].host.confirmed)
            self.assertEqual(fixture.allocations[0].worker.events, ["prepare", "execute", "close", "closed"])
            fixture.allocations[0].generator_host.retire.assert_awaited_once()
            (record,) = await fixture.store.records()
            self.assertIsInstance(record, AcknowledgedAttempt)
            self.assertIs(record.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
            self.assertIsNone(fixture.result)
            self.assertFalse(
                any(
                    request.split(b"\r\n", 1)[0].endswith((b"/fail HTTP/1.1", b"/complete HTTP/1.1"))
                    for request in fixture.api_peer.requests
                )
            )
            self.assertTrue(all(b"PRIVATE_SCIENTIFIC_PROCESS_FAILURE" not in body for body in fixture.api_peer.request_bodies))
            await service.settle()
            self.assertEqual(await fixture.store.records(), (record,))
            self.assertEqual(len(fixture.allocations), 1)
            self.assertEqual(fixture.allocations[0].worker.events.count("execute"), 1)

    async def test_caller_cancellation_keeps_original_api_cause_and_allocates_nothing(self):
        async with ServicePeers() as fixture:
            fixture.api_peer.mode = "headers-stall"
            service = fixture.service()
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.api_peer.received.wait(), 1)
                task.cancel("PRIVATE service cancellation")
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIsInstance(service.last_failure.cause, asyncio.CancelledError)
                self.assertIs(service.last_failure.cause, fixture.api.last_failure.cause.primary)
                self.assertNotIn("PRIVATE", str(service.last_failure))
                self.assertEqual(len(fixture.api_peer.requests), 1)
                self.assertEqual(fixture.allocations, [])
                self.assertEqual(await fixture.store.records(), ())
            finally:
                fixture.stop.set()
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                await service.settle()

    async def test_pending_controller_settlement_is_observed_without_a_second_retirement_call(self):
        async with ServicePeers() as fixture:
            fixture.execution_release = asyncio.Event()
            retirement_entered, release = asyncio.Event(), asyncio.Event()
            service = fixture.service()
            task = asyncio.create_task(service.run(fixture.stop))
            calls = []
            try:
                await asyncio.wait_for(fixture.execution_entered.wait(), 1)
                ports = fixture.allocations[0]
                original = ports.host.retire

                async def retire():
                    calls.append("retire")
                    if len(calls) == 1:
                        raise OSError("PRIVATE first retirement observation")
                    retirement_entered.set()
                    await release.wait()
                    await original()

                ports.host.retire = retire
                fixture.stop.set()
                await asyncio.wait_for(retirement_entered.wait(), 1)
                with self.assertRaises(ServiceFailed):
                    await asyncio.wait_for(task, 1)
                self.assertTrue(service.work_task.done())
                self.assertFalse(ports.host.confirmed)
                self.assertTrue(service.hello_api.retirement_confirmed)
                with self.assertRaises(ServiceFailed):
                    await service.settle()
                self.assertEqual(calls, ["retire", "retire"])
                release.set()
                await service.settle()
                self.assertEqual(calls, ["retire", "retire"])
                self.assertTrue(ports.host.confirmed)
                self.assertEqual(len(fixture.allocations), 1)
                self.assertIsNone(fixture.result)
            finally:
                fixture.stop.set()
                fixture.execution_release.set()
                release.set()
                if not task.done():
                    with suppress(ServiceFailed):
                        await task
                await service.settle()

    async def test_low_headroom_stops_before_hello_or_allocation(self):
        async with ServicePeers() as fixture:
            service = fixture.service()
            with (
                patch("provider_core.attempt_files.os.fstatvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)),
                self.assertRaises(ServiceFailed) as caught,
            ):
                await service.run(fixture.stop)
            self.assertIsInstance(caught.exception.cause, StoreRefused)
            self.assertEqual(fixture.api_peer.requests, [])
            self.assertEqual(fixture.allocations, [])
            self.assertEqual(await fixture.store.records(), ())
            await service.settle()

    async def test_initial_hello_or_started_callback_failure_sends_no_discovery_or_allocation(self):
        for failure in ("hello", "callback"):
            async with ServicePeers() as fixture:
                primary = RuntimeError("PRIVATE started callback")

                if failure == "hello":
                    fixture.hello_failure_after = 0
                    service = fixture.service()
                else:
                    service = fixture.service(on_started=Mock(side_effect=primary))
                with self.assertRaises(ServiceFailed) as caught:
                    await service.run(fixture.stop)
                if failure == "hello":
                    self.assertIsInstance(caught.exception.cause, ApiFailure)
                else:
                    self.assertIs(caught.exception.cause, primary)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(fixture.events, ["inventory", "hello"])
                self.assertEqual(fixture.allocations, [])
                self.assertTrue(service.hello_api.retirement_confirmed)
                await service.settle()

    async def test_factory_retains_interrupted_real_setup_read_until_original_io_settles(self):
        async with ServicePeers() as fixture:
            service = fixture.service()
            entered, release = threading.Event(), threading.Event()
            original = AttemptFiles.records

            def held(files):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("Fixture setup inventory was not released")
                return original(files)

            with patch.object(AttemptFiles, "records", held):
                task = asyncio.create_task(service.run(fixture.stop))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    fixture.stop.set()
                    with self.assertRaises(ServiceFailed):
                        await asyncio.wait_for(task, 1)
                    self.assertFalse(service.work_task.done())
                    self.assertIsNotNone(fixture.store.pending_operation)
                    self.assertIsNone(fixture.store.pending_owner)
                    self.assertEqual(len(fixture.allocations), 1)
                    self.assertNotIn("start", fixture.events)
                    release.set()
                    await service.settle()
                    self.assertIsNone(fixture.store.pending_operation)
                    self.assertTrue(fixture.allocations[0].host.confirmed)
                    self.assertEqual(await fixture.store.records(), ())
                finally:
                    release.set()
                    fixture.stop.set()
                    if not task.done():
                        with suppress(ServiceFailed):
                            await task
                    await service.settle()

    async def test_stop_cancels_active_execution_then_settles_original_controller(self):
        async with ServicePeers() as fixture:
            fixture.execution_release = asyncio.Event()
            service = fixture.service()
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.execution_entered.wait(), 1)
                fixture.stop.set()
                await asyncio.wait_for(task, 2)
                await service.settle()
                self.assertEqual(len(fixture.allocations), 1)
                self.assertTrue(fixture.allocations[0].host.confirmed)
                self.assertIsNone(fixture.result)
                (record,) = await fixture.store.records()
                self.assertIsInstance(record, AcknowledgedAttempt)
                self.assertIs(record.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
            finally:
                fixture.stop.set()
                fixture.execution_release.set()
                if not task.done():
                    await task
                await service.settle()

    async def test_hello_and_scientific_retirement_failures_both_survive_without_replacement(self):
        async with ServicePeers() as fixture:
            fixture.execution_release = asyncio.Event()
            private = OSError("PRIVATE scientific retirement")
            fixture.factory_cleanup_failure = private
            service = fixture.service()
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.execution_entered.wait(), 1)
                fixture.hello_failure_after = 1
                with self.assertRaises(ServiceFailed) as caught:
                    await asyncio.wait_for(task, 2)
                self.assertIsInstance(caught.exception.cause, ApiFailure)
                self.assertGreaterEqual(len(caught.exception.failures), 2)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertIn(("host_retirement", private), service.controller.session.last_retirement_failure.failures)
                self.assertFalse(fixture.allocations[0].host.confirmed)
                self.assertTrue(service.hello_api.retirement_confirmed)
                with self.assertRaises(ServiceRefused):
                    await service.run(asyncio.Event())
                fixture.allocations[0].host.failure = None
                await service.settle()
                self.assertTrue(fixture.allocations[0].host.confirmed)
                self.assertEqual(len(fixture.allocations), 1)
                self.assertIsNone(fixture.result)
            finally:
                fixture.stop.set()
                fixture.execution_release.set()
                for ports in fixture.allocations:
                    ports.host.failure = None
                if not task.done():
                    with suppress(ServiceFailed):
                        await task
                await service.settle()

    async def test_lost_completion_preserves_exact_request_without_rerun_or_automatic_replay(self):
        async with ServicePeers() as fixture:
            fixture.lose_completion = True
            service = fixture.service()
            with self.assertRaises(ServiceFailed):
                await service.run(fixture.stop)
            await service.settle()
            (retained,) = await fixture.store.records()
            self.assertIsInstance(retained, TerminalPending)
            fixture.stop = asyncio.Event()
            replacement = fixture.service()
            with self.assertRaises(ServiceFailed) as caught:
                await replacement.run(fixture.stop)
            self.assertIsInstance(caught.exception.cause, AdmissionHeld)
            await replacement.settle()
            complete_bodies = [
                body
                for request, body in zip(fixture.api_peer.requests, fixture.api_peer.request_bodies, strict=True)
                if b"/complete HTTP" in request.split(b"\r\n", 1)[0]
            ]
            self.assertEqual(complete_bodies, [retained.body])
            self.assertEqual(await fixture.store.records(), (retained,))
            self.assertEqual(len(fixture.allocations), 1)
            self.assertEqual(fixture.allocations[0].worker.events.count("execute"), 1)
            self.assertTrue(fixture.allocations[0].host.confirmed)

    async def test_withdrawal_during_discovery_abandons_selection_before_factory_handoff(self):
        async with ServicePeers() as fixture:
            fixture.discovery_release = asyncio.Event()
            paused = asyncio.Event()

            def current():
                if not fixture.snapshot.analysis_offerings and asyncio.current_task() is service.work_task:
                    paused.set()
                return fixture.snapshot

            service = fixture.service(current_announcement=current)
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.discovery_entered.wait(), 1)
                fixture.snapshot = Announcement("Paused fixture", "No new work", ())

                async def withdrawn():
                    while fixture.hellos[-1]["analysis_offerings"]:
                        fixture.hello_received.clear()
                        await fixture.hello_received.wait()

                await asyncio.wait_for(withdrawn(), 1)
                fixture.discovery_release.set()
                await asyncio.wait_for(paused.wait(), 1)
                self.assertEqual(fixture.allocations, [])
                self.assertNotIn("start", fixture.events)
                self.assertEqual(await fixture.store.records(), ())
            finally:
                fixture.stop.set()
                fixture.discovery_release.set()
                try:
                    await task
                finally:
                    await service.settle()

    async def test_factory_cancellation_and_failed_cleanup_keep_group_and_allow_independent_retirement(self):
        async with ServicePeers() as fixture:
            fixture.factory_release = asyncio.Event()
            fixture.factory_cleanup_failure = OSError("PRIVATE factory retirement")
            service = fixture.service()
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.factory_entered.wait(), 1)
                fixture.stop.set()
                with self.assertRaises(ServiceFailed) as caught:
                    await asyncio.wait_for(task, 1)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertIsInstance(caught.exception.cause, BaseExceptionGroup)
                retirement = fixture.allocations[0].session.last_retirement_failure
                self.assertIs(caught.exception.cause.exceptions[1], retirement)
                self.assertIn(("host_retirement", fixture.factory_cleanup_failure), retirement.failures)
                self.assertTrue(service.hello_api.retirement_confirmed)
                self.assertFalse(fixture.allocations[0].host.confirmed)
                await service.settle()
                with self.assertRaises(ServiceRefused):
                    await service.run(asyncio.Event())
                self.assertEqual(len(fixture.allocations), 1)
            finally:
                fixture.stop.set()
                fixture.factory_release.set()
                for ports in fixture.allocations:
                    ports.host.failure = None
                    await ports.session.retire()
                if not task.done():
                    with suppress(ServiceFailed):
                        await task
                await service.settle()

    async def test_cancelled_api_cleanup_still_attempts_independent_hello_retirement(self):
        async with ServicePeers() as fixture:
            fixture.stop.set()
            service = fixture.service()
            cancellation = asyncio.CancelledError("PRIVATE cleanup interruption")
            try:
                with (
                    patch.object(fixture.api, "retire", new=AsyncMock(side_effect=cancellation)),
                    patch.object(service.hello_api, "retire", new=AsyncMock(wraps=service.hello_api.retire)) as hello_retirement,
                ):
                    with self.assertRaises(asyncio.CancelledError):
                        await service.run(fixture.stop)
                    hello_retirement.assert_awaited_once_with(owner=service)
                self.assertTrue(any(cause is cancellation for _, cause in service.last_failure.failures))
                self.assertNotIn("PRIVATE", str(service.last_failure))
            finally:
                await service.settle()

    async def test_remote_work_holds_before_hello_discovery_or_allocation(self):
        async with ServicePeers() as fixture:
            fixture.inventory_replies = [
                inventory_response(attempts=[attempt(analysis_kind_ref="another_kind")], next_cursor=None)
            ]
            service = fixture.service()
            with self.assertRaises(ServiceFailed) as caught:
                await service.run(fixture.stop)
            self.assertIsInstance(caught.exception.cause, AdmissionHeld)
            self.assertEqual(fixture.events, ["inventory"])
            self.assertEqual(fixture.allocations, [])
            self.assertEqual(await fixture.store.records(), ())
            await service.settle()

    async def test_late_factory_return_after_stop_is_retired_without_starting(self):
        async with ServicePeers() as fixture:
            fixture.factory_release = asyncio.Event()
            fixture.resist_factory_cancel = True
            service = fixture.service()
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.factory_entered.wait(), 1)
                fixture.stop.set()
                await asyncio.wait_for(fixture.factory_cancelled.wait(), 1)
                with self.assertRaises(ServiceFailed):
                    await asyncio.wait_for(task, 1)
                self.assertFalse(service.work_task.done())
                with self.assertRaises(ServiceRefused):
                    await service.run(asyncio.Event())
                fixture.factory_release.set()
                await service.settle()
                self.assertEqual(len(fixture.allocations), 1)
                self.assertEqual(fixture.allocations[0].worker.events, ["close", "closed"])
                self.assertTrue(fixture.allocations[0].host.confirmed)
                self.assertNotIn("start", fixture.events)
                self.assertEqual(await fixture.store.records(), ())
            finally:
                fixture.stop.set()
                fixture.factory_release.set()
                if not task.done():
                    with suppress(ServiceFailed):
                        await task
                await service.settle()
