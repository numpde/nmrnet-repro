"""Live exclusions and delivery duties survive waits; restart grants no action."""

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import patch

from tests.fakes.attempt_inventory import attempt
from tests.integration.nmrnet.service_ports import ServicePeers
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.runtime import assemble_attempt
from nmrnet_provider.service import ServiceFailed
from provider_core.attempt_record import StartPending
from provider_core.attempt_request import start_body
from provider_core.controller import LiveStartPolicy, StartDeferred
from provider_core.delivery_client import DeliveryUnavailable


class LiveServiceOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_stop_interrupts_live_start_backoff_without_using_the_drain_to_replay(self):
        async with ServicePeers() as fixture:
            starts = []
            original_reply, original_arguments = fixture.api_peer.response_factory, fixture.arguments

            async def reply(request, body):
                if request.startswith(b"POST /provider/v1/execution-attempts/start "):
                    starts.append(body)
                    await original_reply(request, body)
                    return None
                return await original_reply(request, body)

            def arguments(pending):
                return original_arguments(pending) | {"start_policy": LiveStartPolicy(
                    0.01, lambda *_: fixture.stop.set(), lambda *_: self.fail("Stopped START cannot recover"),
                    maximum_replays=1,
                )}

            fixture.api_peer.response_factory = reply
            service = fixture.service(drain_seconds=3)
            with patch.object(fixture, "arguments", arguments):
                with self.assertRaises(ServiceFailed):
                    await asyncio.wait_for(service.run(fixture.stop), 2)
            await fixture.settle_service(service)
            self.assertEqual(len(starts), 1)
            self.assertTrue(fixture.attempt_stopping.is_set())
            self.assertTrue(service.shutdown_ready)
            self.assertTrue(fixture.claims.owns_job(fixture.config.lifecycle.scope, fixture.ports.instructions.job_ref))
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertEqual(fixture.allocations[0].worker.events, [])

    async def test_old_remote_attempt_does_not_block_unrelated_fresh_science_or_trigger_reconciliation(self):
        async with ServicePeers() as fixture:
            fixture.remote_attempts["old"] = attempt(job_ref="job:old-dark-lifetime")
            service = fixture.service()
            fixture.stack.push_async_callback(fixture.settle_service, service)
            await asyncio.wait_for(service.run(fixture.stop), 4)
            await fixture.wait_published()
            self.assertTrue(service.shutdown_ready)
            self.assertEqual(len(fixture.allocations), 1)
            self.assertEqual(fixture.allocations[0].worker.events.count("execute"), 1)
            self.assertNotIn("/provider/v1/execution-attempts", fixture.request_paths)
            self.assertIn("old", fixture.remote_attempts)
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())

    async def test_uncertain_start_remains_excluded_but_quiescent_service_can_stop(self):
        async with ServicePeers() as fixture:
            starts = []
            original_reply = fixture.api_peer.response_factory

            async def reply(request, body):
                if request.startswith(b"POST /provider/v1/execution-attempts/start "):
                    starts.append(body)
                    return None
                return await original_reply(request, body)

            fixture.api_peer.response_factory = reply
            pending = await fixture.discover()
            fixture.ports = SessionPorts()
            arguments = fixture.arguments(pending)
            arguments["start_policy"] = LiveStartPolicy(0.01, lambda *_: None, lambda *_: None, maximum_replays=0)
            controller = await assemble_attempt(**arguments)
            fixture.stack.push_async_callback(fixture.settle_controller, controller)
            outcome = await controller.run(pending)
            self.assertIs(type(outcome), StartDeferred)
            self.assertTrue(outcome.may_have_committed)
            self.assertTrue(controller.shutdown_ready)
            self.assertTrue(fixture.claims.owns_job(pending.scope, pending.input.job_ref))
            service = fixture.service(on_started=fixture.stop.set)
            fixture.stack.push_async_callback(fixture.settle_service, service)
            with self.assertRaises(ServiceFailed):
                await asyncio.wait_for(service.run(fixture.stop), 3)
            self.assertTrue(service.shutdown_ready)
            self.assertEqual(len(starts), 1)
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertEqual(fixture.ports.worker.events, [])

    async def test_full_live_registry_paces_without_start_or_service_failure(self):
        async with ServicePeers(maximum_live_attempts=1) as fixture:
            selected = await fixture.discover()
            other = replace(selected.input, job_ref="job:other-live-owner")
            held = StartPending(selected.scope, selected.analysis_kind_ref, other, start_body(other.job_ref, "held:one"))
            fixture.ports = SessionPorts()
            owner = await assemble_attempt(**fixture.arguments(held))
            fixture.stack.push_async_callback(fixture.settle_controller, owner)
            fixture.claims.claim_live(held, lifecycle_owner=owner)
            service = fixture.service()
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                async with asyncio.timeout(3):
                    while len(fixture.allocations) < 2:
                        await asyncio.sleep(0.01)
                fixture.stop.set()
                await asyncio.wait_for(task, 2)
                self.assertIsNone(service.last_failure)
                self.assertTrue(service.shutdown_ready)
                self.assertNotIn("start", fixture.events)
                self.assertEqual(fixture.model_peer.requests, [])
                self.assertTrue(fixture.claims.owns_job(held.scope, held.input.job_ref))
            finally:
                fixture.stop.set()
                await asyncio.gather(task, return_exceptions=True)
                await fixture.settle_service(service)

    async def test_unacknowledged_selected_result_keeps_service_held_after_physical_cleanup(self):
        async with ServicePeers() as fixture:
            controllers = []
            delivery_returned = asyncio.Event()
            original = fixture.open_attempt

            async def open_attempt(pending, *, stopping):
                controller = await original(pending, stopping=stopping)
                controllers.append(controller)

                async def unavailable(outcome):
                    delivery_returned.set()
                    raise DeliveryUnavailable("Authored unacknowledged delivery", status=400)

                controller.delivery.submit = unavailable
                return controller

            service = fixture.service(open_attempt=open_attempt)
            try:
                with self.assertRaises(ServiceFailed):
                    await asyncio.wait_for(service.run(fixture.stop), 4)
                await asyncio.wait_for(delivery_returned.wait(), 1)
                (controller,) = controllers
                self.assertTrue(controller.work_settled)
                self.assertFalse(controller.shutdown_ready)
                self.assertFalse(service.shutdown_ready)
                self.assertIsNone(fixture.result)
                self.assertEqual(fixture.allocations[0].worker.events.count("execute"), 1)
            finally:
                # The permanent refusal has no fabricated ACK or retry authority.
                # Physical cleanup completes, but the authored process stays held.
                with self.assertRaises(ServiceFailed):
                    await service.settle()
            self.assertFalse(service.shutdown_ready)
            self.assertEqual(fixture.allocations[0].worker.events.count("execute"), 1)
