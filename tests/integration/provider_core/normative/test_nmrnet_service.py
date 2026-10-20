"""Service ordering reaches the original controller without preallocated science."""

import asyncio
import json
import unittest

from tests.integration.nmrnet.interpreter_ports import tool_reply
from tests.integration.nmrnet.service_ports import ServicePeers
from tests.integration.nmrnet.test_live_worker import verify_signed_request

from nmrnet_provider.service import Announcement, ServiceFailed
from provider_core.attempt_record import TerminalConfirmed, TerminalOperation, TerminalPending
from provider_core.attempt_store import StoreRefused


class ProviderServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_report_reaches_the_failure_recipient_with_safe_actionable_guidance(self):
        cases = (
            (
                "source_unit_unclear",
                "The interpreter could not determine the coordinate unit. Specify angstrom, nm or bohr in the request instructions.",
            ),
            (
                "conformer_count_unclear",
                "The interpreter could not determine how many conformers to generate. Specify a positive whole-number conformer count in the request instructions.",
            ),
            (
                "record_selection_unclear",
                "The interpreter could not determine which uploaded records to use. Specify all records, or their record numbers in the required order.",
            ),
            ("other_input_problem", "The interpreter reported that it could not select supported molecular input."),
        )
        for issue, guidance in cases:
            with self.subTest(issue=issue):
                await self.check_model_report(issue, guidance)

    async def check_model_report(self, issue, guidance):
        report = "PRIVATE_SOURCE_EXCERPT " + "x" * 1500
        expected_message = guidance + " This was not independently verified; its explanation is retained in protected evidence."
        received = []
        async with ServicePeers() as fixture:

            async def model_report(request, _body):
                self.assertIn(b"authorization: bearer private_model_key\r\n", request.lower())
                self.assertNotIn(b"signature:", request.lower())
                return tool_reply("report", "report_input_problem", {"issue": issue, "message": report})

            async def recipient(request, body):
                if request.split(b"\r\n", 1)[0].endswith(b"/fail HTTP/1.1"):
                    self.assertTrue(request.startswith(b"POST "))
                    self.assertTrue(fixture.allocations[0].host.confirmed)
                    self.assertEqual(fixture.allocations[0].worker.events, ["close", "closed"])
                    (pending,) = await fixture.store.records()
                    self.assertIs(type(pending), TerminalPending)
                    self.assertEqual(pending.body, body)
                    received.append(body)
                    reply = await fixture.api_reply(request, body)
                    fixture.stop.set()
                    return reply
                return await fixture.api_reply(request, body)

            fixture.model_peer.response_factory = model_report
            fixture.api_peer.response_factory = recipient
            service = fixture.service()
            fixture.stack.push_async_callback(service.settle)
            await asyncio.wait_for(service.run(fixture.stop), 3)
            self.assertEqual(len(received), 1)
            wire = json.loads(received[0])
            self.assertEqual(wire["failure_code"], "interpreter_reported_input_problem")
            self.assertEqual(wire["failure_message"], expected_message)
            self.assertNotIn(b"PRIVATE_SOURCE_EXCERPT", received[0])
            self.assertNotIn(b"private_model_key", received[0].lower())
            (confirmed,) = await fixture.store.records()
            self.assertIs(type(confirmed), TerminalConfirmed)
            self.assertIs(confirmed.pending.operation, TerminalOperation.FAIL)
            self.assertEqual(confirmed.pending.body, received[0])
            self.assertEqual(confirmed.receipt.failure_code, wire["failure_code"])
            self.assertEqual(confirmed.receipt.failure_message, expected_message)
            evidence = json.loads(confirmed.pending.evidence.content)["model_report"]
            self.assertEqual(evidence["issue"], issue)
            self.assertEqual(evidence["explanation"], report)
            self.assertIs(evidence["independently_verified"], False)
            self.assertEqual(evidence["interpretation"]["outcome"], "model_reported_input_problem")
            self.assertEqual(len(fixture.model_peer.requests), 1)
            fixture.allocations[0].allocate_reader.assert_not_awaited()
            fixture.allocations[0].allocate_generator.assert_not_awaited()
            self.assertEqual(fixture.store_peer.requests, [])
            self.assertIsNone(fixture.result)
            for request, body in zip(fixture.api_peer.requests, fixture.api_peer.request_bodies, strict=True):
                verify_signed_request(request, body, origin=fixture.api_peer.origin, key=fixture.config.api.identity.private_key)
                self.assertNotIn(b"authorization:", request.lower())
                self.assertNotIn(b"private_model_key", request.lower())

    async def test_stop_drains_the_original_execution_and_publication_when_it_finishes(self):
        async with ServicePeers() as fixture:
            fixture.execution_release = asyncio.Event()
            service = fixture.service(drain_seconds=1)
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.execution_entered.wait(), 1)
                fixture.stop.set()
                fixture.execution_release.set()
                await asyncio.wait_for(task, 2)
                await service.settle()
                (record,) = await fixture.store.records()
                self.assertIsInstance(record, TerminalConfirmed)
                self.assertEqual(fixture.allocations[0].worker.events.count("execute"), 1)
                self.assertTrue(fixture.allocations[0].host.confirmed)
            finally:
                fixture.stop.set()
                fixture.execution_release.set()
                if not task.done():
                    await task
                await service.settle()

    async def test_confirmed_record_saturation_prevents_another_allocation_without_deleting_history(self):
        async with ServicePeers(maximum_records=1) as fixture:
            first = fixture.service()
            await first.run(fixture.stop)
            before = await fixture.store.records()
            requests = tuple(fixture.api_peer.requests)
            fixture.stop = asyncio.Event()
            replacement = fixture.service()
            with self.assertRaises(ServiceFailed) as caught:
                await replacement.run(fixture.stop)
            self.assertIsInstance(caught.exception.cause, StoreRefused)
            self.assertEqual(await fixture.store.records(), before)
            self.assertEqual(tuple(fixture.api_peer.requests), requests)
            self.assertEqual(len(fixture.allocations), 1)
            await replacement.settle()

    async def test_already_stopped_service_allocates_and_sends_nothing(self):
        async with ServicePeers() as fixture:
            fixture.stop.set()
            service = fixture.service()
            await service.run(fixture.stop)
            await service.settle()
            self.assertEqual(fixture.allocations, [])
            self.assertEqual(fixture.api_peer.requests, [])
            self.assertEqual(await fixture.store.records(), ())

    async def test_initial_admission_and_hello_precede_fresh_allocation_and_exact_lifecycle(self):
        async with ServicePeers() as fixture:
            service = fixture.service()
            await asyncio.wait_for(service.run(fixture.stop), 3)
            await service.settle()
            self.assertEqual(fixture.events[:7], ["inventory", "hello", "started", "inventory", "discover", "allocate", "start"])
            self.assertEqual(len(fixture.allocations), 1)
            ports = fixture.allocations[0]
            self.assertEqual(ports.worker.events.count("execute"), 1)
            self.assertTrue(ports.host.confirmed)
            (record,) = await fixture.store.records()
            self.assertIsInstance(record, TerminalConfirmed)
            self.assertEqual(record.pending.attempt.start.input, ports.instructions.identity)
            for request, body in zip(fixture.api_peer.requests, fixture.api_peer.request_bodies, strict=True):
                verify_signed_request(request, body, origin=fixture.api_peer.origin, key=fixture.config.api.identity.private_key)
                self.assertNotIn(b"authorization:", request.lower())
            self.assertTrue(service.hello_api.retirement_confirmed)

    async def test_hello_refresh_uses_current_snapshot_while_scientific_execution_is_blocked(self):
        async with ServicePeers() as fixture:
            fixture.execution_release = asyncio.Event()
            service = fixture.service()
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.execution_entered.wait(), 2)
                fixture.snapshot = Announcement("Changed current name", "Current description", ())

                async def refreshed():
                    while not any(item["display_name"] == "Changed current name" for item in fixture.hellos):
                        fixture.hello_received.clear()
                        await fixture.hello_received.wait()

                await asyncio.wait_for(refreshed(), 1)
                self.assertFalse(task.done())
                self.assertEqual(fixture.hellos[-1]["analysis_offerings"], [])
                self.assertEqual(len(fixture.allocations), 1)
                fixture.execution_release.set()
                await asyncio.wait_for(task, 2)
                self.assertTrue(fixture.allocations[0].host.confirmed)
            finally:
                fixture.stop.set()
                fixture.execution_release.set()
                if not task.done():
                    await task
                await service.settle()
