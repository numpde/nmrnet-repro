"""Service ordering reaches the original controller without preallocated science."""

import asyncio
import json
import unittest
from dataclasses import replace

from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.interpreter_ports import tool_reply
from tests.integration.nmrnet.service_ports import ServicePeers
from tests.integration.nmrnet.test_live_worker import verify_signed_request

from nmrnet_provider.service import Announcement, ServiceFailed
from provider_core.attempt_record import LocalExecutionPhase, TerminalConfirmed, TerminalOperation, TerminalPending
from provider_core.attempt_store import AttemptStore, StoreRefused


class ProviderServiceTests(unittest.IsolatedAsyncioTestCase):
    async def fallback_endpoints(self, fixture):
        """Use real carrier refusals before the existing scripted successful peer."""
        unavailable = await fixture.stack.enter_async_context(
            TlsPeer(
                "success",
                response_status=503,
                response_body=b"PRIVATE_MODEL_BODY",
                response_headers=((b"x-request-id", b"request:prior-endpoint"), (b"x-private", b"PRIVATE_HEADER")),
            )
        )
        (working,) = fixture.config.interpreter.endpoints
        settings = replace(
            fixture.config.interpreter,
            max_turns=3,
            endpoints=(
                replace(working, spec=replace(working.spec, configuration_id="too-small"), request_bytes=1),
                replace(
                    working,
                    spec=replace(working.spec, configuration_id="unavailable", url=unavailable.origin + "/chat"),
                    http=replace(working.http, trust=unavailable.trust),
                ),
                working,
            ),
        )
        fixture.config = replace(fixture.config, interpreter=settings)
        return unavailable

    def require_prior_endpoint_evidence(self, content):
        history = json.loads(content)["interpreter_diagnostics"]
        self.assertEqual([item["endpoint_index"] for item in history], [0, 1])
        rendered = json.dumps(history)
        for fact in ('"reason": "request_limit"', '"reason": "http_status"', '"status": 503', "request:prior-endpoint"):
            self.assertIn(fact, rendered)
        self.assertNotIn("PRIVATE", rendered)
        return history

    async def test_successful_fallback_is_durable_before_prediction_and_retained_after_completion(self):
        async with ServicePeers() as fixture:
            unavailable = await self.fallback_endpoints(fixture)
            original = fixture.open_attempt
            durable = []

            async def open_attempt(pending):
                controller = await original(pending)
                worker = fixture.allocations[-1].worker
                prepare = worker.prepare

                async def require_retained_entry(requests):
                    (entered,) = await fixture.store.records()
                    self.assertIs(entered.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
                    durable.append(self.require_prior_endpoint_evidence(entered.prepared_identity.content))
                    return await prepare(requests)

                worker.prepare = require_retained_entry
                return controller

            service = fixture.service(open_attempt=open_attempt)
            fixture.stack.push_async_callback(service.settle)
            await asyncio.wait_for(service.run(fixture.stop), 3)
            (confirmed,) = await fixture.store.records()
            self.assertIs(type(confirmed), TerminalConfirmed)
            self.assertEqual(self.require_prior_endpoint_evidence(confirmed.pending.evidence.content), durable[0])
            self.assertNotIn("interpreter_diagnostics", json.dumps(fixture.result))
            self.assertNotIn("request:prior-endpoint", json.dumps(fixture.result))
            self.assertEqual(fixture.allocations[0].worker.events, ["prepare", "execute", "close", "closed"])
            fixture.allocations[0].allocate_generator.assert_awaited_once()
            self.assertEqual(len(unavailable.requests), 1)
            self.assertEqual(len(fixture.model_peer.requests), 1)

    async def test_fallback_evidence_survives_fresh_store_reopen_without_starting_prediction(self):
        async with ServicePeers() as fixture:
            await self.fallback_endpoints(fixture)
            original = fixture.open_attempt

            async def open_attempt(pending):
                controller = await original(pending)

                async def stop_before_prediction(_requests):
                    raise OSError("Authored process-boundary interruption before worker preparation")

                fixture.allocations[-1].worker.prepare = stop_before_prediction
                return controller

            service = fixture.service(open_attempt=open_attempt)
            with self.assertRaises(ServiceFailed):
                await asyncio.wait_for(service.run(fixture.stop), 3)
            await service.settle()
            await fixture.store.close()
            settings = fixture.config.lifecycle
            reopened = AttemptStore(
                settings.store_root, scope=settings.scope, limits=settings.store_limits, operation_seconds=settings.store_seconds
            )
            await reopened.open()
            try:
                (entered,) = await reopened.records()
                self.assertIs(entered.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
                self.require_prior_endpoint_evidence(entered.prepared_identity.content)
            finally:
                await reopened.close()
            self.assertEqual(fixture.allocations[0].worker.events, ["close", "closed"])
            self.assertIsNone(fixture.result)

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
            await self.fallback_endpoints(fixture)

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
            self.require_prior_endpoint_evidence(confirmed.pending.evidence.content)
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
