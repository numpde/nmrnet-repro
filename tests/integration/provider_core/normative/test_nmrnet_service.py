"""Service ordering reaches the original controller without preallocated science."""

import asyncio
import json
import unittest
from dataclasses import replace
from urllib.parse import urlsplit

from tests.fakes.api import response_headers
from tests.fakes.attempt_inventory import response as inventory_response
from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.admission_ports import feed_response
from tests.integration.nmrnet.interpreter_ports import tool_reply
from tests.integration.nmrnet.service_ports import ServicePeers
from tests.integration.nmrnet.test_live_worker import verify_signed_request

from nmrnet_provider.service import Announcement, ServiceFailed
from provider_core.attempt_record import LocalExecutionPhase, TerminalConfirmed, TerminalOperation, TerminalPending
from provider_core.attempt_store import AttemptStore, StoreRefused


class ProviderServiceTests(unittest.IsolatedAsyncioTestCase):
    def hello_outage(self, fixture, *, failed_send):
        """One signed Hello receives maintenance; retain each sent current body."""
        sent = []

        async def response(request, body):
            fixture.api_peer.response_status = 200
            fixture.api_peer.response_headers = response_headers()
            if request.split(b"\r\n", 1)[0] == b"POST /provider/v1/hello HTTP/1.1":
                verify_signed_request(request, body, origin=fixture.api_peer.origin, key=fixture.config.api.identity.private_key)
                sent.append(json.loads(body))
                if len(sent) == failed_send:
                    if failed_send == 2:
                        await fixture.execution_entered.wait()
                    fixture.api_peer.response_status = 503
                    fixture.api_peer.response_headers = response_headers(status=503, request_id=b"request:hello-outage")
                    return b"PRIVATE maintenance body"
            return await fixture.api_reply(request, body)

        fixture.api_peer.response_factory = response
        return sent

    async def test_initial_hello_outage_sends_the_latest_snapshot_before_starting_one_job(self):
        async with ServicePeers() as fixture:
            sent = self.hello_outage(fixture, failed_send=1)
            notices = []

            def retry(kind, failure, seconds):
                self.assertEqual(kind, "hello")
                self.assertTrue(service.hello_api.retirement_confirmed)
                self.assertEqual(fixture.allocations, [])
                self.assertNotIn("started", fixture.events)
                fixture.snapshot = replace(fixture.snapshot, description="Current after outage")
                notices.append((failure, seconds))

            service = fixture.service(on_api_retry=retry)
            service.policy = replace(service.policy, hello_seconds=30)
            fixture.stack.push_async_callback(service.settle)
            await asyncio.wait_for(service.run(fixture.stop), 3)
            self.assertEqual(len(sent), 2)
            self.assertNotEqual(sent[0]["description"], sent[1]["description"])
            self.assertEqual(sent[1]["description"], "Current after outage")
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0][0].request_id, "request:hello-outage")
            self.assertIsNone(service.unconfirmed_hello)
            self.assertEqual(fixture.events.count("started"), 1)
            self.assertEqual(len(fixture.allocations), 1)
            self.assertEqual(fixture.allocations[0].worker.events, ["prepare", "execute", "close", "closed"])

    async def test_periodic_hello_recovers_with_withdrawal_while_the_original_prediction_keeps_running(self):
        async with ServicePeers() as fixture:
            fixture.execution_release = asyncio.Event()
            sent = self.hello_outage(fixture, failed_send=2)
            recovered = asyncio.Event()

            def retry(kind, _failure, _seconds):
                self.assertEqual(kind, "hello")
                self.assertTrue(fixture.execution_entered.is_set())
                fixture.snapshot = replace(fixture.snapshot, analysis_offerings=())

            service = fixture.service(on_api_retry=retry, on_api_recovered=lambda _kind, _failure: recovered.set())
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.execution_entered.wait(), 1)
                await asyncio.wait_for(recovered.wait(), 1)
                self.assertFalse(task.done())
                self.assertFalse(fixture.stop.is_set())
                self.assertEqual(sent[1]["analysis_offerings"], sent[0]["analysis_offerings"])
                self.assertEqual(sent[2]["analysis_offerings"], [])
                self.assertIsNone(service.unconfirmed_hello)
                self.assertEqual(len(fixture.allocations), 1)
                fixture.stop.set()
                fixture.execution_release.set()
                await asyncio.wait_for(task, 2)
                self.assertEqual(fixture.allocations[0].worker.events, ["prepare", "execute", "close", "closed"])
                self.assertEqual(fixture.events.count("start"), 1)
            finally:
                fixture.stop.set()
                fixture.execution_release.set()
                await asyncio.gather(task, return_exceptions=True)
                await service.settle()

    async def test_stop_during_initial_hello_backoff_keeps_uncertainty_without_admitting_work(self):
        async with ServicePeers() as fixture:
            sent = self.hello_outage(fixture, failed_send=1)
            waiting = asyncio.Event()
            service = fixture.service(on_api_retry=lambda _kind, _failure, _seconds: waiting.set())
            service.policy = replace(service.policy, idle_seconds=1, api_backoff_max_seconds=1)
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                fixture.stop.set()
                await asyncio.wait_for(task, 0.3)
                self.assertEqual(len(sent), 1)
                self.assertEqual(fixture.events, ["inventory"])
                self.assertEqual(fixture.allocations, [])
                self.assertEqual(await fixture.store.records(), ())
                self.assertEqual(service.unconfirmed_hello.request_id, "request:hello-outage")
                self.assertIsNone(service.last_failure)
            finally:
                fixture.stop.set()
                await asyncio.gather(task, return_exceptions=True)
                await service.settle()

    def read_outages(self, fixture, failed_targets):
        """Return HTTP maintenance replies for selected signed GETs, before science.

        These tests defer periodic Hello to isolate read recovery's observations.
        """
        observed = []

        async def response(request, body):
            method, target, _version = request.split(b"\r\n", 1)[0].split(b" ")
            observed.append(target.decode())
            fixture.api_peer.response_status = 200
            fixture.api_peer.response_headers = response_headers()
            if method == b"GET" and failed_targets and target.decode() == failed_targets[0]:
                failed_targets.pop(0)
                fixture.api_peer.response_status = 503
                fixture.api_peer.response_headers = response_headers(status=503, request_id=b"request:read-outage")
                return b"PRIVATE maintenance body"
            return await fixture.api_reply(request, body)

        fixture.api_peer.response_factory = response
        return observed

    async def test_startup_read_outage_recovers_before_one_job_and_one_scientific_execution(self):
        async with ServicePeers() as fixture:
            inventory = "/provider/v1/execution-attempts?state=in_progress&limit=2"
            observed = self.read_outages(fixture, [inventory] * 4)
            notices = []

            def retry(kind, failure, seconds):
                self.assertEqual(kind, "read")
                self.assertTrue(fixture.api.retirement_confirmed)
                self.assertEqual(fixture.allocations, [])
                notices.append(("retry", failure, seconds))

            service = fixture.service(
                on_api_retry=retry, on_api_recovered=lambda _kind, failure: notices.append(("recovered", failure))
            )
            service.policy = replace(service.policy, hello_seconds=30)
            fixture.stack.push_async_callback(service.settle)
            await asyncio.wait_for(service.run(fixture.stop), 3)
            self.assertEqual([item[0] for item in notices], ["retry"] * 4 + ["recovered"])
            for notice, ceiling in zip(notices[:4], (0.05, 0.1, 0.2, 0.2), strict=True):
                self.assertGreaterEqual(notice[2], ceiling / 2)
                self.assertLessEqual(notice[2], ceiling)
            self.assertIs(notices[3][1], notices[4][1])
            self.assertEqual(notices[0][1].request_id, "request:read-outage")
            self.assertEqual(observed[:5], [inventory] * 5)
            self.assertEqual(len(fixture.allocations), 1)
            self.assertEqual(fixture.allocations[0].worker.events, ["prepare", "execute", "close", "closed"])
            fixture.allocations[0].allocate_generator.assert_awaited_once()
            self.assertIsNone(service.last_failure)

    async def test_discovery_page_outage_restarts_complete_admission_and_first_page(self):
        async with ServicePeers() as fixture:
            fixture.inventory_replies.append(inventory_response(attempts=[], next_cursor=None))
            fixture.feed_replies = [feed_response(next_cursor="YQ")]
            first_page = "/provider/v1/jobs?analysis_kind_ref=peaks_from_mol&has_provider_execution_attempt=false&limit=2"
            second_page = first_page + "&cursor=YQ"
            observed = self.read_outages(fixture, [second_page])

            def retry(kind, _failure, _seconds):
                self.assertEqual(kind, "read")
                self.assertEqual(fixture.allocations, [])
                fixture.feed_replies = None

            service = fixture.service(on_api_retry=retry, on_api_recovered=lambda _kind, _failure: None)
            service.policy = replace(service.policy, hello_seconds=30)
            fixture.stack.push_async_callback(service.settle)
            await asyncio.wait_for(service.run(fixture.stop), 3)
            discovery = [target for target in observed if urlsplit(target).path == "/provider/v1/jobs"]
            self.assertEqual(discovery, [first_page, second_page, first_page])
            self.assertEqual(fixture.events.count("inventory"), 3)
            self.assertEqual(fixture.events.count("start"), 1)
            self.assertEqual(fixture.allocations[0].worker.events.count("execute"), 1)

    async def test_stop_during_read_backoff_sends_no_more_requests_or_allocations(self):
        async with ServicePeers() as fixture:
            inventory = "/provider/v1/execution-attempts?state=in_progress&limit=2"
            observed = self.read_outages(fixture, [inventory])
            waiting = asyncio.Event()
            service = fixture.service(
                on_api_retry=lambda _kind, _failure, _seconds: waiting.set(),
                on_api_recovered=lambda _kind, _failure: self.fail("No recovery"),
            )
            service.policy = replace(service.policy, idle_seconds=1, api_backoff_max_seconds=1)
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                fixture.stop.set()
                await asyncio.wait_for(task, 0.3)
                self.assertEqual(observed, [inventory])
                self.assertEqual(fixture.allocations, [])
                self.assertEqual(await fixture.store.records(), ())
                self.assertIsNone(service.last_failure)
            finally:
                fixture.stop.set()
                await asyncio.gather(task, return_exceptions=True)
                await service.settle()

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
                "The language model could not determine the coordinate unit. Specify angstrom, nm or bohr in the request instructions.",
            ),
            (
                "conformer_count_unclear",
                "The language model could not determine how many conformers to generate from SMILES. "
                "Specify a positive whole-number conformer count in the request instructions.",
            ),
            (
                "record_selection_unclear",
                "The language model could not determine which attached structures to use. "
                "Identify the file and specify all records, or list the record or PDB model numbers in the required order.",
            ),
            (
                "other_input_problem",
                "The language model reported that it could not select molecular input. "
                "Provide coordinate text with units, an attached file identifying the intended structures, "
                "or SMILES with an explicit conformer count.",
            ),
        )
        for issue, guidance in cases:
            with self.subTest(issue=issue):
                await self.check_model_report(issue, guidance)

    async def check_model_report(self, issue, guidance):
        report = "PRIVATE_SOURCE_EXCERPT " + "x" * 1500
        expected_message = (
            guidance
            + " This report was not independently verified. The provider retains the model's explanation as private evidence."
        )
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
