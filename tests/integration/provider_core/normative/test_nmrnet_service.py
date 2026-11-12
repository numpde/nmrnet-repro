"""Service ordering reaches the original controller without preallocated science."""

import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from tests.fakes.api import response_headers, signature_window_problem
from tests.fakes.api_tls import verify_signed_request
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.unix_proxy import UnixProxy
from tests.integration.nmrnet.admission_ports import feed_response
from tests.integration.nmrnet.interpreter_ports import tool_reply
from tests.integration.nmrnet.service_ports import ServicePeers

from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.runtime import capture_prose_services
from nmrnet_provider.service import Announcement, ServiceFailed
from provider_core.api import ProviderAPI
from provider_core.controller import ControllerFailed
from provider_core.delivery_client import DeliveryClient
from provider_core.execution import ExecutionFailed
from provider_core.interpreter import InterpretationFailed, ModelReportedInterpretationProblem


class ProviderServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limited_background_calls_respect_retry_after_and_stop(self):
        for route, expected_notice_kind in (("jobs", "read"), ("hello", "hello")):
            with self.subTest(route=route):
                async with ServicePeers() as fixture:
                    notices = []

                    # silent-defaults: allow-contract -- Capture this case's route and peer before asynchronous callbacks run.
                    async def response(request, body, *, route=route, fixture=fixture):
                        fixture.api_peer.response_status = 200
                        fixture.api_peer.response_headers = response_headers()
                        target = request.split(b"\r\n", 1)[0].split(b" ")[1].split(b"?", 1)[0]
                        if target == ("/provider/v1/" + route).encode():
                            fixture.api_peer.response_status = 429
                            fixture.api_peer.response_headers = (*response_headers(status=429), (b"retry-after", b"60"))
                            return b"rate limited"
                        return await fixture.api_reply(request, body)

                    # silent-defaults: allow-contract -- Capture this case's notice collection and stop owner for the registered retry callback.
                    def retry(kind, failure, seconds, *, notices=notices, fixture=fixture):
                        notices.append((kind, failure.status, seconds))
                        fixture.stop.set()

                    fixture.api_peer.response_factory = response
                    service = fixture.service(on_api_retry=retry)
                    fixture.stack.push_async_callback(fixture.settle_service, service)
                    await asyncio.wait_for(service.run(fixture.stop), 3)
                    self.assertEqual(1, len(notices))
                    self.assertEqual(notices[0][:2], (expected_notice_kind, 429))
                    self.assertGreaterEqual(notices[0][2], 60)
                    self.assertEqual([], fixture.allocations)

    async def test_connect_gateway_outage_recovers_in_the_same_service_before_any_scientific_allocation(self):
        async with ServicePeers() as fixture, UnixProxy(fixture.api_peer.origin, "refuse", refusal_status=502) as proxy:
            settings = fixture.config.api
            fixture.config = replace(fixture.config, api=replace(settings, http=replace(settings.http, proxy_socket=proxy.path)))
            fixture.api = ProviderAPI(
                settings.identity, fixture.config.api.http.new_transport(), expected_topology=settings.expected_topology
            )
            notices = []

            def recovered_route(kind, failure, _seconds):
                self.assertEqual(kind, "hello")
                self.assertIsNone(failure.status)
                self.assertEqual(502, failure.cause.proxy_connect_status)
                notices.append(failure)
                proxy.mode = "tunnel"

            service = fixture.service(on_api_retry=recovered_route, on_started=fixture.stop.set)
            await asyncio.wait_for(service.run(fixture.stop), 4)
            await fixture.settle_service(service)
            self.assertIsNone(service.last_failure)
            self.assertEqual(1, len(notices))
            self.assertEqual(2, len(proxy.headers))
            self.assertEqual(1, len(fixture.api_peer.requests))
            self.assertEqual(1, len(fixture.hellos))
            self.assertEqual([], fixture.allocations)
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())

    async def test_timing_refusals_preserve_the_existing_operation_owners(self):
        for code in ("authentication_window_closed", "signature_expired"):
            for route, refusal_count, expected_notice_kind in (
                ("jobs", 3, "read"),
                ("hello", 3, "hello"),
                ("execution-attempts/start", 1, None),
                ("execution-attempts/complete", 1, None),
            ):
                with self.subTest(code=code, route=route):
                    await self.assert_timing_refresh(
                        route, code, refusal_count=refusal_count, expected_notice_kind=expected_notice_kind
                    )

    async def assert_timing_refresh(self, route, code, *, refusal_count, expected_notice_kind):
        async with ServicePeers() as fixture:
            requests = []
            target = "/provider/v1/" + route

            async def response(request, body):
                fixture.api_peer.response_status = 200
                fixture.api_peer.response_headers = response_headers()
                path = urlsplit(request.split(b"\r\n", 1)[0].split(b" ")[1].decode()).path
                if path == target:
                    verify_signed_request(
                        request, body, origin=fixture.api_peer.origin, key=fixture.config.api.identity.private_key
                    )
                    requests.append((request, body))
                    if len(requests) <= refusal_count:
                        fixture.api_peer.response_status = 401
                        fixture.api_peer.response_headers = response_headers(status=401, request_id=b"request:window")
                        return signature_window_problem("request:window", code=code)
                return await fixture.api_reply(request, body)

            fixture.api_peer.response_factory = response
            service = fixture.service()
            fixture.stack.push_async_callback(fixture.settle_service, service)
            try:
                await asyncio.wait_for(service.run(fixture.stop), fixture.config.preparation.seconds)
            except ServiceFailed as failure:
                self.fail(json.dumps(failure_evidence(failure, secrets=())))
            self.assertGreaterEqual(len(requests), refusal_count + 1)
            signatures = [
                next(line for line in request.split(b"\r\n") if line.lower().startswith(b"signature-input:"))
                for request, _body in requests[: refusal_count + 1]
            ]
            self.assertEqual(len(set(signatures)), refusal_count + 1)
            self.assertEqual(requests[0][1], requests[1][1], msg="A timing refusal must not change a retained write body.")
            self.assertEqual(1, fixture.events.count("start"))
            self.assertEqual(1, len(fixture.model_peer.requests))
            self.assertEqual(1, len(fixture.allocations))
            self.assertEqual(1, fixture.allocations[0].worker.events.count("execute"))
            if expected_notice_kind is not None:
                self.assertEqual(
                    [notice[:2] for notice in fixture.api_notices],
                    [("retry", expected_notice_kind)] * refusal_count + [("recovered", expected_notice_kind)],
                )
                self.assertEqual("request:window", fixture.api_notices[0][2].request_id)

    async def test_active_read_outage_recovers_without_restarting_the_original_interpreter_or_prediction(self):
        async with ServicePeers() as fixture:
            fixture.execution_release = asyncio.Event()
            outage = None

            async def response(request, body):
                nonlocal outage
                fixture.api_peer.response_status = 200
                fixture.api_peer.response_headers = response_headers()
                target = request.split(b"\r\n", 1)[0]
                if (
                    target.startswith(b"GET /provider/v1/execution-attempts/execution_attempt:")
                    and fixture.execution_entered.is_set()
                ):
                    verify_signed_request(
                        request, body, origin=fixture.api_peer.origin, key=fixture.config.api.identity.private_key
                    )
                    if outage is None:
                        outage = request
                        fixture.api_peer.response_status = 503
                        fixture.api_peer.response_headers = response_headers(status=503, request_id=b"request:active-outage")
                        return b"PRIVATE maintenance"
                    fixture.execution_release.set()
                return await fixture.api_reply(request, body)

            fixture.api_peer.response_factory = response
            service = fixture.service()
            service.policy = replace(service.policy, hello_seconds=30)
            fixture.stack.push_async_callback(fixture.settle_service, service)
            try:
                await asyncio.wait_for(service.run(fixture.stop), fixture.config.preparation.seconds)
                self.assertIsNotNone(outage)
                self.assertEqual(1, fixture.events.count("start"))
                self.assertEqual(1, len(fixture.model_peer.requests))
                self.assertEqual(1, len(fixture.allocations))
                self.assertEqual(["prepare", "execute", "close", "closed"], fixture.allocations[0].worker.events)
                self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
                self.assertEqual(
                    [notice[:2] for notice in fixture.api_notices], [("retry", "observation"), ("recovered", "observation")]
                )
                self.assertIs(fixture.api_notices[0][2], fixture.api_notices[1][2])
                self.assertEqual("request:active-outage", fixture.api_notices[0][2].request_id)
                self.assertEqual(fixture.api_notices[0][-1].job_ref, fixture.ports.instructions.job_ref)
            finally:
                fixture.execution_release.set()

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
                        await asyncio.wait_for(fixture.execution_entered.wait(), fixture.config.preparation.seconds)
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
                self.assertEqual([], fixture.allocations)
                self.assertNotIn("started", fixture.events)
                fixture.snapshot = replace(fixture.snapshot, description="Current after outage")
                notices.append((failure, seconds))

            service = fixture.service(on_api_retry=retry)
            service.policy = replace(service.policy, hello_seconds=30)
            fixture.stack.push_async_callback(fixture.settle_service, service)
            await asyncio.wait_for(service.run(fixture.stop), fixture.config.preparation.seconds)
            self.assertEqual(2, len(sent))
            self.assertNotEqual(sent[0]["description"], sent[1]["description"])
            self.assertEqual("Current after outage", sent[1]["description"])
            self.assertEqual(1, len(notices))
            self.assertEqual("request:hello-outage", notices[0][0].request_id)
            self.assertIsNone(service.unconfirmed_hello)
            self.assertEqual(1, fixture.events.count("started"))
            self.assertEqual(1, len(fixture.allocations))
            self.assertEqual(["prepare", "execute", "close", "closed"], fixture.allocations[0].worker.events)

    async def test_periodic_hello_recovers_with_withdrawal_while_the_original_prediction_keeps_running(self):
        async with ServicePeers() as fixture:
            fixture.execution_release = asyncio.Event()
            sent = self.hello_outage(fixture, failed_send=2)
            recovered = asyncio.Event()

            def retry(kind, _failure, _seconds):
                self.assertEqual(kind, "hello")
                self.assertTrue(fixture.execution_entered.is_set())
                fixture.snapshot = replace(fixture.snapshot, analysis_offerings=())

            service = fixture.service(
                hello_seconds=0.05, on_api_retry=retry, on_api_recovered=lambda _kind, _failure: recovered.set()
            )
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.execution_entered.wait(), fixture.config.preparation.seconds)
                await asyncio.wait_for(recovered.wait(), 1)
                self.assertFalse(task.done())
                self.assertFalse(fixture.stop.is_set())
                self.assertEqual(sent[1]["analysis_offerings"], sent[0]["analysis_offerings"])
                self.assertEqual([], sent[2]["analysis_offerings"])
                self.assertIsNone(service.unconfirmed_hello)
                self.assertEqual(1, len(fixture.allocations))
                fixture.execution_release.set()
                await asyncio.wait_for(fixture.completed.wait(), 2)
                await asyncio.wait_for(task, 2)
                self.assertEqual(["prepare", "execute", "close", "closed"], fixture.allocations[0].worker.events)
                self.assertEqual(1, fixture.events.count("start"))
            finally:
                fixture.stop.set()
                fixture.execution_release.set()
                await asyncio.gather(task, return_exceptions=True)
                await fixture.settle_service(service)

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
                self.assertEqual(1, len(sent))
                self.assertEqual([], fixture.events)
                self.assertEqual([], fixture.allocations)
                self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
                self.assertEqual("request:hello-outage", service.unconfirmed_hello.request_id)
                self.assertIsNone(service.last_failure)
            finally:
                fixture.stop.set()
                await asyncio.gather(task, return_exceptions=True)
                await fixture.settle_service(service)

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

    async def test_discovery_read_outage_recovers_before_one_job_and_one_scientific_execution(self):
        async with ServicePeers() as fixture:
            inventory = "/provider/v1/jobs?analysis_kind_ref=peaks_from_mol&has_provider_execution_attempt=false&limit=2"
            observed = self.read_outages(fixture, [inventory] * 4)
            notices = []

            def retry(kind, failure, seconds):
                self.assertEqual(kind, "read")
                self.assertTrue(fixture.api.retirement_confirmed)
                self.assertEqual([], fixture.allocations)
                notices.append(("retry", failure, seconds))

            service = fixture.service(
                on_api_retry=retry, on_api_recovered=lambda _kind, failure: notices.append(("recovered", failure))
            )
            service.policy = replace(service.policy, hello_seconds=30)
            fixture.stack.push_async_callback(fixture.settle_service, service)
            await asyncio.wait_for(service.run(fixture.stop), fixture.config.preparation.seconds)
            self.assertEqual([item[0] for item in notices], ["retry"] * 4 + ["recovered"])
            for notice, ceiling in zip(notices[:4], (0.05, 0.1, 0.2, 0.2), strict=True):
                self.assertGreaterEqual(notice[2], ceiling / 2)
                self.assertLessEqual(notice[2], ceiling)
            self.assertIs(notices[3][1], notices[4][1])
            self.assertEqual("request:read-outage", notices[0][1].request_id)
            self.assertEqual([target for target in observed if target.startswith("/provider/v1/jobs")][:5], [inventory] * 5)
            self.assertEqual(1, len(fixture.allocations))
            self.assertEqual(["prepare", "execute", "close", "closed"], fixture.allocations[0].worker.events)
            fixture.allocations[0].allocate_generator.assert_awaited_once()
            self.assertIsNone(service.last_failure)

    async def test_discovery_page_outage_restarts_complete_admission_and_first_page(self):
        async with ServicePeers() as fixture:
            fixture.feed_replies = [feed_response(next_cursor="YQ")]
            first_page = "/provider/v1/jobs?analysis_kind_ref=peaks_from_mol&has_provider_execution_attempt=false&limit=2"
            second_page = first_page + "&cursor=YQ"
            observed = self.read_outages(fixture, [second_page])

            def retry(kind, _failure, _seconds):
                self.assertEqual(kind, "read")
                self.assertEqual([], fixture.allocations)
                fixture.feed_replies = None

            service = fixture.service(on_api_retry=retry, on_api_recovered=lambda _kind, _failure: None)
            service.policy = replace(service.policy, hello_seconds=30)
            fixture.stack.push_async_callback(fixture.settle_service, service)
            await asyncio.wait_for(service.run(fixture.stop), fixture.config.preparation.seconds)
            # Initial recovery ends at START; relay publication may overlap
            # later idle polling after the producer has handed off its result.
            initial_reads = observed[: observed.index("/provider/v1/execution-attempts/start")]
            discovery = [target for target in initial_reads if urlsplit(target).path == "/provider/v1/jobs"]
            self.assertEqual(discovery, [first_page, second_page, first_page])
            self.assertNotIn("/provider/v1/execution-attempts", fixture.request_paths)
            self.assertEqual(1, fixture.events.count("start"))
            self.assertEqual(1, fixture.allocations[0].worker.events.count("execute"))

    async def test_stop_during_read_backoff_sends_no_more_requests_or_allocations(self):
        async with ServicePeers() as fixture:
            inventory = "/provider/v1/jobs?analysis_kind_ref=peaks_from_mol&has_provider_execution_attempt=false&limit=2"
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
                self.assertEqual(observed, ["/provider/v1/hello", inventory])
                self.assertEqual([], fixture.allocations)
                self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
                self.assertIsNone(service.last_failure)
            finally:
                fixture.stop.set()
                await asyncio.gather(task, return_exceptions=True)
                await fixture.settle_service(service)

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
            endpoints=(
                replace(
                    working,
                    spec=replace(working.spec, configuration_id="unavailable", url=unavailable.origin + "/chat"),
                    trust=unavailable.trust,
                ),
                working,
            ),
        )
        fixture.config = replace(fixture.config, interpreter=settings)
        fixture.prose_services = capture_prose_services(fixture.config)
        return unavailable

    def require_prior_endpoint_evidence(self, content, *, report=False):
        history = json.loads(content)["interpreter_diagnostics"]
        self.assertIn(0, [item.get("endpoint_index") for item in history])
        rendered = json.dumps(history)
        for fact in ('"reason": "http_status"', '"status": 503', "request:prior-endpoint"):
            self.assertIn(fact, rendered)
        if not report:
            self.assertNotIn("PRIVATE", rendered)
        return history

    async def test_successful_fallback_identity_is_bound_before_prediction_without_persisting_private_evidence(self):
        async with ServicePeers() as fixture:
            unavailable = await self.fallback_endpoints(fixture)
            original = fixture.open_attempt
            durable = []

            async def open_attempt(pending, *, stopping):
                controller = await original(pending, stopping=stopping)
                worker = fixture.allocations[-1].worker
                prepare = worker.prepare

                async def require_retained_entry(requests):
                    self.assertTrue(fixture.claims.owns_job(pending.scope, pending.input.job_ref))
                    identity = fixture.allocations[-1].preparation_outcome.identity
                    durable.append(self.require_prior_endpoint_evidence(identity.content))
                    return await prepare(requests)

                worker.prepare = require_retained_entry
                return controller

            service = fixture.service(open_attempt=open_attempt)
            fixture.stack.push_async_callback(fixture.settle_service, service)
            await asyncio.wait_for(service.run(fixture.stop), fixture.config.preparation.seconds)
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
            identity = fixture.allocations[-1].preparation_outcome.identity
            self.assertEqual(self.require_prior_endpoint_evidence(identity.content), durable[0])
            self.assertNotIn("interpreter_diagnostics", json.dumps(fixture.result))
            self.assertNotIn("request:prior-endpoint", json.dumps(fixture.result))
            self.assertEqual(["prepare", "execute", "close", "closed"], fixture.allocations[0].worker.events)
            fixture.allocations[0].allocate_generator.assert_awaited_once()
            self.assertEqual(2, len(unavailable.requests))
            self.assertEqual(
                [(index, type(failure).__name__) for index, failure in fixture.operator_notices],
                [(0, "ConversationFailed")],
            )
            self.assertEqual(1, len(fixture.model_peer.requests))

    async def test_model_report_retains_original_operator_handoff_without_terminal_report(self):
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
        async with ServicePeers() as fixture:
            await self.fallback_endpoints(fixture)

            async def model_report(request, _body):
                self.assertIn(b"authorization: bearer private_model_key\r\n", request.lower())
                self.assertNotIn(b"signature:", request.lower())
                return tool_reply("report", "report_interpretation_problem", {"issue": issue, "message": report})

            fixture.model_peer.response_factory = model_report
            service = fixture.service()
            fixture.stack.push_async_callback(fixture.settle_service, service)
            with self.assertRaises(ServiceFailed) as caught:
                await asyncio.wait_for(service.run(fixture.stop), fixture.config.preparation.seconds)
            (controller,) = fixture.claims.live_owners()
            self.assertIs(caught.exception.cause, controller.last_failure)
            self.assertIs(type(controller.last_failure), ControllerFailed)
            execution = controller.last_failure.cause
            self.assertIs(type(execution), ExecutionFailed)
            preparation = controller.session._input_preparation
            original = preparation.chooser_failure
            self.assertIs(execution.cause, original)
            self.assertIs(type(original), InterpretationFailed)
            _, model_report = original.failures[-1]
            self.assertIs(type(model_report), ModelReportedInterpretationProblem)
            self.assertEqual(issue, model_report.__cause__.issue)
            self.assertEqual(report, model_report.__cause__.explanation)
            self.assertEqual(
                guidance
                + " This report was not independently verified. The provider retains the model's explanation as private evidence.",
                str(model_report.__cause__),
            )
            self.assertTrue(controller.shutdown_ready)
            self.assertTrue(fixture.claims.owns_job(fixture.config.lifecycle.scope, fixture.allocations[0].instructions.job_ref))
            self.assertFalse(any(path.endswith(("/fail", "/complete")) for path in fixture.request_paths))
            self.assertEqual([], fixture.allocations[0].worker.events)
            self.assertFalse(fixture.allocations[0].host.confirmed)
            self.assertEqual(1, len(fixture.allocations))
            self.assertEqual(1, len(fixture.model_peer.requests))
            fixture.allocations[0].allocate_reader.assert_not_awaited()
            fixture.allocations[0].allocate_generator.assert_not_awaited()
            self.assertEqual([], fixture.store_peer.requests)
            self.assertIsNone(fixture.result)
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
            diagnostics = preparation.interpreter_diagnostics
            self.require_prior_endpoint_evidence(
                json.dumps({"interpreter_diagnostics": json.loads(diagnostics)}).encode(), report=True
            )

    async def test_stop_drains_the_original_execution_and_publication_when_it_finishes(self):
        async with ServicePeers() as fixture:
            fixture.execution_release = asyncio.Event()
            service = fixture.service(drain_seconds=1)
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.execution_entered.wait(), fixture.config.preparation.seconds)
                fixture.stop.set()
                fixture.execution_release.set()
                await asyncio.wait_for(task, 2)
                await fixture.settle_service(service)
                self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
                await fixture.wait_published()
                self.assertEqual(1, fixture.allocations[0].worker.events.count("execute"))
                self.assertTrue(fixture.allocations[0].host.confirmed)
            finally:
                fixture.stop.set()
                fixture.execution_release.set()
                if not task.done():
                    await task
                await fixture.settle_service(service)

    async def test_already_stopped_service_allocates_and_sends_nothing(self):
        async with ServicePeers() as fixture:
            fixture.stop.set()
            service = fixture.service()
            await service.run(fixture.stop)
            await fixture.settle_service(service)
            self.assertEqual([], fixture.allocations)
            self.assertEqual([], fixture.api_peer.requests)
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())

    async def test_initial_admission_and_hello_precede_fresh_allocation_and_exact_lifecycle(self):
        async with ServicePeers() as fixture:
            original_reply = fixture.api_peer.response_factory

            async def verify_reply(request, body):
                verify_signed_request(request, body, origin=fixture.api_peer.origin, key=fixture.config.api.identity.private_key)
                self.assertNotIn(b"authorization:", request.lower())
                return await original_reply(request, body)

            fixture.api_peer.response_factory = verify_reply
            service = fixture.service()
            service.policy = replace(service.policy, hello_seconds=30)
            with patch.object(
                DeliveryClient, "pending", AsyncMock(side_effect=AssertionError("outbox reads are unavailable")), create=True
            ):
                await asyncio.wait_for(service.run(fixture.stop), fixture.config.preparation.seconds)
            await fixture.settle_service(service)
            self.assertEqual(["hello", "started", "discover", "allocate", "start"], fixture.events[:5])
            self.assertEqual(1, len(fixture.allocations))
            ports = fixture.allocations[0]
            self.assertEqual(1, ports.worker.events.count("execute"))
            self.assertTrue(ports.host.confirmed)
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
            await fixture.wait_published()
            self.assertEqual(fixture.result["input_fingerprint"], ports.instructions.identity.input_fingerprint)
            self.assertTrue(service.hello_api.retirement_confirmed)

    async def test_hello_refresh_uses_current_snapshot_while_scientific_execution_is_blocked(self):
        async with ServicePeers() as fixture:
            fixture.execution_release = asyncio.Event()
            service = fixture.service(hello_seconds=0.05)
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.execution_entered.wait(), fixture.config.preparation.seconds)
                fixture.snapshot = Announcement("Changed current name", "Current description", ())

                async def refreshed():
                    while not any(item["display_name"] == "Changed current name" for item in fixture.hellos):
                        fixture.hello_received.clear()
                        await fixture.hello_received.wait()

                await asyncio.wait_for(refreshed(), 1)
                self.assertFalse(task.done())
                self.assertEqual([], fixture.hellos[-1]["analysis_offerings"])
                self.assertEqual(1, len(fixture.allocations))
                fixture.execution_release.set()
                await asyncio.wait_for(task, 2)
                self.assertTrue(fixture.allocations[0].host.confirmed)
            finally:
                fixture.stop.set()
                fixture.execution_release.set()
                if not task.done():
                    await task
                await fixture.settle_service(service)
