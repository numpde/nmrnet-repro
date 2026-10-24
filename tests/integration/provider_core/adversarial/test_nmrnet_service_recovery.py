"""Restart observes compact control and exact relay ownership without new science."""

import asyncio
import threading
import unittest
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.fakes.attempts import observation, start_response
from tests.integration.nmrnet.service_ports import ServicePeers

from nmrnet_provider.config import ANALYSIS_KIND_REF
from nmrnet_provider.service import ServiceFailed
from provider_core.api import ProviderAPI
from provider_core.attempt_record import StartPending, TerminalOperation
from provider_core.attempt_request import fail_body, start_body
from provider_core.attempt_response import read_start
from provider_core.checkpoint_store import CheckpointStore
from provider_core.delivery_store import DeliveryOutcome
from provider_core.ownership import OwnershipRefused
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, DeliveryAcceptedCheckpoint, EnteredCheckpoint, EvidenceDigest
from provider_core.recovery import AttemptRecovery, RecoveryFailed, RecoveryRefused


async def retain_ack(fixture, key="recovery:one"):
    pending = StartPending(fixture.store.scope, ANALYSIS_KIND_REF, fixture.ports.instructions.identity,
                           start_body(fixture.ports.instructions.job_ref, key))
    await fixture.store.retain_start(pending)
    admitted = read_start(start_response(analysis_kind_ref=ANALYSIS_KIND_REF), job_ref=pending.input.job_ref,
                          analysis_kind_ref=pending.analysis_kind_ref, provider_ref=pending.scope.provider_ref)
    return await fixture.store.acknowledge_start(pending, admitted)


async def retain_selection(fixture):
    # Leave the real inbox available but stop its API worker, so this receiving
    # row remains durable while producer restart observes its handoff identity.
    for worker in fixture.relay.workers.values():
        worker.request_stop()
    await asyncio.gather(*fixture.relay._worker_tasks)
    acknowledged = await retain_ack(fixture)
    reference = acknowledged.admission.execution_attempt_ref
    item = DeliveryOutcome(fixture.store.scope.api_origin, fixture.store.scope.provider_ref,
                           acknowledged.start.input.job_ref, reference, TerminalOperation.FAIL,
                           fail_body(reference, "provider_interrupted", "Authored retained outcome."))
    selected = await fixture.store.select_terminal(acknowledged, item)
    fixture.relay.queue.enqueue(item)
    return selected, item


class InterruptedServiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_observes_real_relay_row_only_after_native_barrier_without_republishing(self):
        async with ServicePeers() as fixture:
            selected, item = await retain_selection(fixture)
            barrier = AsyncMock()
            service = fixture.service(retire_native=barrier, on_started=fixture.stop.set)
            read = service.delivery.status

            async def status(reference):
                barrier.assert_awaited_once()
                self.assertEqual(await fixture.store.inventory(), (selected,))
                return await read(reference)

            with patch.object(service.delivery, "status", side_effect=status):
                await asyncio.wait_for(service.run(fixture.stop), 3)
            await service.settle()
            self.assertEqual(await fixture.store.inventory(), ())
            self.assertEqual(fixture.relay.queue.next_pending(item.api_origin, item.provider_ref), item)
            self.assertEqual(fixture.allocations, [])
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertTrue(all(request.startswith(b"GET ") or request.startswith(b"POST /provider/v1/hello ")
                                for request in fixture.api_peer.requests))

    async def test_failed_native_barrier_retains_control_and_queue_without_reading_relay_or_api(self):
        async with ServicePeers() as fixture:
            selected, item = await retain_selection(fixture)
            barrier = AsyncMock(side_effect=TimeoutError("Original native ownership remains held"))
            service = fixture.service(retire_native=barrier)
            with patch.object(service.delivery, "status", wraps=service.delivery.status) as status:
                with self.assertRaises(ServiceFailed):
                    await service.run(fixture.stop)
                status.assert_not_awaited()
            await service.settle()
            self.assertEqual(await fixture.store.inventory(), (selected,))
            self.assertEqual(fixture.relay.queue.next_pending(item.api_origin, item.provider_ref), item)
            self.assertEqual(fixture.api_peer.requests, [])
            self.assertEqual(fixture.allocations, [])

    async def test_stop_during_native_proof_keeps_exact_selected_control_and_grants_no_read(self):
        async with ServicePeers() as fixture:
            selected, item = await retain_selection(fixture)
            entered, release = asyncio.Event(), asyncio.Event()

            async def barrier():
                entered.set()
                await release.wait()

            service = fixture.service(retire_native=barrier)
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                fixture.stop.set()
                await asyncio.wait_for(task, 2)
                await service.settle()
                self.assertEqual(await fixture.store.inventory(), (selected,))
                self.assertEqual(fixture.relay.queue.next_pending(item.api_origin, item.provider_ref), item)
                self.assertEqual(fixture.api_peer.requests, [])
                self.assertEqual(fixture.allocations, [])
            finally:
                release.set()
                fixture.stop.set()
                await asyncio.gather(task, return_exceptions=True)
                await service.settle()

    async def test_stop_during_real_relay_status_read_preserves_original_control_and_no_enqueue(self):
        async with ServicePeers() as fixture:
            selected, item = await retain_selection(fixture)
            entered, release = asyncio.Event(), asyncio.Event()
            respond = fixture.relay.inbox._respond

            async def blocked(connection, writer, status, document):
                if type(document) is dict and document.get("execution_attempt_ref") == item.execution_attempt_ref:
                    entered.set()
                    await release.wait()
                await respond(connection, writer, status, document)

            fixture.relay.inbox._respond = blocked
            service = fixture.service()
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                fixture.stop.set()
                with self.assertRaises(ServiceFailed):
                    await asyncio.wait_for(task, 2)
                await service.settle()
                self.assertEqual(await fixture.store.inventory(), (selected,))
                self.assertEqual(fixture.relay.queue.next_pending(item.api_origin, item.provider_ref), item)
                self.assertEqual(fixture.api_peer.requests, [])
                self.assertEqual(fixture.allocations, [])
                self.assertTrue(service.delivery.retirement_confirmed)
            finally:
                release.set()
                fixture.stop.set()
                await asyncio.gather(task, return_exceptions=True)
                await service.settle()

    async def test_stop_during_original_inventory_finishes_read_without_starting_native_proof(self):
        for retained in (False, True):
            with self.subTest(retained=retained):
                async with ServicePeers() as fixture:
                    expected = (await retain_ack(fixture),) if retained else ()
                    fixture.store._seconds = 0.1
                    entered, release = threading.Event(), threading.Event()
                    read = fixture.store._database.inventory

                    def held_read():
                        entered.set()
                        if not release.wait(3):
                            raise TimeoutError("Fixture did not release its original inventory read")
                        return read()

                    barrier = AsyncMock()
                    service = fixture.service(retire_native=barrier, drain_seconds=0.01)
                    with patch.object(fixture.store._database, "inventory", side_effect=held_read) as original:
                        task = asyncio.create_task(service.run(fixture.stop))
                        try:
                            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                            worker = fixture.store._pending.worker
                            fixture.stop.set()
                            with self.assertRaises(ServiceFailed):
                                await asyncio.wait_for(task, 1)
                            self.assertTrue(service.work_task.done())
                            self.assertIsNotNone(fixture.store.pending_operation)
                            self.assertIs(fixture.store._pending.worker, worker)
                            self.assertFalse(worker.done())
                            release.set()
                            await service.settle()
                            self.assertTrue(worker.done())
                            self.assertEqual(original.call_count, 1)
                            barrier.assert_not_awaited()
                            self.assertEqual(fixture.api_peer.requests, [])
                            self.assertEqual(fixture.allocations, [])
                        finally:
                            release.set()
                            await asyncio.gather(task, return_exceptions=True)
                            await service.settle()
                    self.assertEqual(await fixture.store.inventory(), expected)

    async def test_cancelled_acceptance_write_is_settled_once_without_another_relay_command(self):
        async with ServicePeers() as fixture:
            selected, item = await retain_selection(fixture)
            fixture.store._seconds = 0.1
            entered, release = threading.Event(), threading.Event()
            write = fixture.store._database.accept_delivery

            def held_write(*args):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("Fixture did not release original handoff metadata commit")
                return write(*args)

            service = fixture.service(drain_seconds=0.01)
            with patch.object(fixture.store._database, "accept_delivery", side_effect=held_write) as original:
                task = asyncio.create_task(service.run(fixture.stop))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    fixture.stop.set()
                    with self.assertRaises(ServiceFailed):
                        await asyncio.wait_for(task, 2)
                    self.assertIsNotNone(fixture.store.pending_operation)
                    release.set()
                    await service.settle()
                    self.assertEqual(original.call_count, 1)
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    await service.settle()
            (accepted,) = await fixture.store.inventory()
            self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
            self.assertEqual(accepted.selection, selected)
            fixture.stop = asyncio.Event()
            replacement = fixture.service(on_started=fixture.stop.set)
            await asyncio.wait_for(replacement.run(fixture.stop), 3)
            await replacement.settle()
            self.assertEqual(await fixture.store.inventory(), ())
            self.assertEqual(fixture.relay.queue.next_pending(item.api_origin, item.provider_ref), item)
            self.assertEqual(fixture.allocations, [])

    async def test_aliased_restored_inventory_reserves_nothing_and_makes_no_effect(self):
        async with ServicePeers(maximum_records=2) as fixture:
            first = await retain_ack(fixture)
            second = await retain_ack(fixture, key="recovery:alias")
            before = await fixture.store.inventory()
            barrier = AsyncMock()
            service = fixture.service(retire_native=barrier)
            with self.assertRaises(ServiceFailed) as refused:
                await service.run(fixture.stop)
            self.assertIs(type(refused.exception.cause), OwnershipRefused)
            await service.settle()
            barrier.assert_not_awaited()
            self.assertEqual(await fixture.store.inventory(), before)
            self.assertEqual(fixture.api_peer.requests, [])
            for record in (first, second):
                claim = fixture.claims.claim_record(record)
                claim.release()

    async def test_wrong_context_refuses_before_inventory_native_or_api_effect(self):
        async with ServicePeers() as fixture:
            for reason in ("identity", "topology", "scope"):
                with self.subTest(reason=reason):
                    api, store = fixture.api, fixture.store
                    if reason != "scope":
                        api = ProviderAPI(replace(fixture.config.api.identity) if reason == "identity" else fixture.config.api.identity,
                                          fixture.config.api.http.new_transport(),
                                          expected_topology="web" if reason == "topology" else fixture.config.api.expected_topology)
                    else:
                        root = fixture.files.root / "foreign-checkpoints"
                        root.mkdir(mode=0o700)
                        store = CheckpointStore(root, scope=replace(fixture.store.scope, generation_id="generation:foreign"),
                                                limits=fixture.config.lifecycle.store_limits, operation_seconds=1)
                        await store.open()
                    barrier = AsyncMock()
                    service = fixture.service(api=api, store=store, retire_native=barrier)
                    try:
                        with patch.object(store, "inventory", wraps=store.inventory) as read:
                            with self.assertRaises(ServiceFailed):
                                await service.run(asyncio.Event())
                            read.assert_not_awaited()
                        await service.settle()
                        barrier.assert_not_awaited()
                        self.assertEqual(fixture.api_peer.requests, [])
                    finally:
                        await api.retire()
                        if store is not fixture.store:
                            await store.close()

    async def test_unknown_entered_work_reads_api_then_holds_without_science_or_terminal_send(self):
        async with ServicePeers() as fixture:
            acknowledged = await retain_ack(fixture)
            entered = await fixture.store.mark_entered(acknowledged, EvidenceDigest("fixture.prepared.v1", "sha256:" + "a" * 64, 1))
            barrier = AsyncMock()
            service = fixture.service(retire_native=barrier)
            with self.assertRaises(ServiceFailed) as refused:
                await service.run(fixture.stop)
            self.assertIs(type(refused.exception.cause), RecoveryFailed)
            self.assertIs(type(refused.exception.cause.cause), RecoveryRefused)
            await service.settle()
            barrier.assert_awaited_once()
            self.assertEqual(await fixture.store.inventory(), (entered,))
            self.assertEqual(len(fixture.api_peer.requests), 1)
            self.assertTrue(fixture.api_peer.requests[0].startswith(b"GET "))
            self.assertEqual(fixture.allocations, [])
            self.assertEqual(fixture.hellos, [])

    async def test_explicit_recovery_shared_claim_blocks_startup_until_original_io_settles(self):
        async with ServicePeers() as fixture:
            record = await retain_ack(fixture)
            (claim,) = fixture.claims.claim_records((record,))
            entered, release = asyncio.Event(), asyncio.Event()
            reply = fixture.api_peer.response_factory

            async def blocked(request, body):
                if request.startswith(b"GET /provider/v1/execution-attempts/execution_attempt:"):
                    entered.set()
                    await release.wait()
                return await reply(request, body)

            fixture.api_peer.response_factory = blocked
            service = fixture.service()
            recovery = AttemptRecovery(fixture.api, fixture.store, claim, delivery=service.delivery)
            task = asyncio.create_task(recovery.recover(record))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                with self.assertRaises(ServiceFailed) as refused:
                    await service.run(asyncio.Event())
                self.assertIs(type(refused.exception.cause), OwnershipRefused)
                self.assertEqual(fixture.allocations, [])
                self.assertEqual(len(fixture.api_peer.requests), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                if claim.operation_owner is not None:
                    await recovery.settle()
                claim.release()
                await service.settle()
                self.assertEqual(await fixture.store.inventory(), (record,))
            finally:
                release.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                if claim.active:
                    if claim.operation_owner is not None:
                        await recovery.settle()
                    claim.release()
                await service.settle()
