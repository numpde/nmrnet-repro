"""The operator sees safe causal and effect evidence from the actual process owner."""

import asyncio
import errno
import hashlib
import json
import os
import signal
import threading
import unittest
from contextlib import nullcontext, redirect_stderr
from dataclasses import fields, replace
from unittest.mock import patch

from tests.fakes.api import response_headers
from tests.fakes.attempt_store import admission, record
from tests.fakes.attempts import ATTEMPT_REF, progress_response
from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.process_ports import ProcessPeers
from tests.integration.nmrnet.test_live_worker import verify_signed_request

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import PreparationRetirementFailed
from nmrnet_provider.native_factory import NativeAttemptFactory
from nmrnet_provider.process import _runtime_facts, _settle_borrowers, _write_api_notice, route_provider_config, run_provider
from nmrnet_provider.process_reports import DiagnosticOutputRefused, ProviderProcessFailed, failure_evidence
from nmrnet_provider.service import ProviderService, ServiceFailed
from nmrnet_provider.worker_client import PredictionExecutionFailed
from nmrnet_provider.worker_protocol import WorkerProtocolError
from provider_core.api import ApiFailure, ProviderAPI
from provider_core.attempt_record import acknowledge_start
from provider_core.attempt_store import AttemptStore
from provider_core.controller import ControllerFailed, StartRecovery, TerminalRecovery
from provider_core.conversation import ConversationRetirementFailed
from provider_core.execution import ConditionRecovery, ExecutionFailed, ObservationRecovery
from provider_core.ownership import AttemptClaims
from provider_core.transport import HttpResponse


class ForegroundProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_condition_recovery_receipt_precedes_one_real_model_post(self):
        async with TlsPeer("body-stall") as model, ProcessPeers(model_peer=model) as fixture:
            (fixture.diagnostics / "inject-condition-before-input").write_bytes(b"test port\n")
            original = fixture.peer.response_factory
            condition_sends = []

            async def response(request, body):
                fixture.peer.response_status = 200
                fixture.peer.response_headers = response_headers(request_id=b"request:process-proof")
                result = await original(request, body)
                if request.startswith(b"PUT ") and json.loads(body)["condition_code"] == "waiting_capacity":
                    self.assertEqual(model.requests, [], "The condition receipt must precede the model POST")
                    condition_sends.append((request, body))
                    if len(condition_sends) == 1:
                        fixture.peer.response_status = 503
                        fixture.peer.response_headers = response_headers(status=503, request_id=b"request:condition-outage")
                        return b"PRIVATE_MODEL_KEY private_key=PRIVATE_CREDENTIAL maintenance"
                    self.assertEqual(len(condition_sends), 2, "No retry after the matching receipt")
                    return progress_response(phase="preparing", condition_code="waiting_capacity")
                return result

            fixture.peer.response_factory = response
            process = await fixture.start(child_script="interrupted_model_process.py")
            retry = json.loads(await asyncio.wait_for(process.stderr.readline(), 3))
            recovered = json.loads(await asyncio.wait_for(process.stderr.readline(), 3))
            self.assertEqual([retry["event"], recovered["event"]], ["api_condition_retry", "api_condition_recovered"])
            self.assertIs(retry["condition_receipt_received"], False)
            self.assertIs(recovered["condition_receipt_received"], True)
            self.assertGreater(retry["retry_seconds"], 0)
            for notice in (retry, recovered):
                self.assertEqual(notice["phase"], "preparing")
                self.assertEqual(notice["condition_code"], "waiting_capacity")
                self.assertEqual(notice["job_ref"], fixture.ports.instructions.job_ref)
                self.assertEqual(notice["execution_attempt_ref"], ATTEMPT_REF)
                rendered = json.dumps(notice)
                self.assertIn("request:condition-outage", rendered)
                self.assertNotIn("PRIVATE", rendered)
            await fixture.observed("condition-ack-received")
            await fixture.observed("model-head-received")
            self.assertEqual(len(model.requests), 1)
            self.assertEqual(len(condition_sends), 2)
            self.assertEqual(condition_sends[0][1], condition_sends[1][1])
            self.assertNotEqual(condition_sends[0][0], condition_sends[1][0], "Fresh signed request for the same report")
            process.send_signal(signal.SIGTERM)
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 1, stderr.decode())
            self.assertEqual(len(model.requests), 1)
            self.assertFalse((fixture.diagnostics / "science-entered").exists())

    async def test_unconfirmed_phase_notice_precedes_fresh_read_and_one_real_model_post(self):
        async with TlsPeer("body-stall") as model, ProcessPeers(model_peer=model) as fixture:
            original = fixture.peer.response_factory
            failed = False

            async def response(request, body):
                nonlocal failed
                fixture.peer.response_status = 200
                fixture.peer.response_headers = response_headers(request_id=b"request:process-proof")
                result = await original(request, body)
                if (
                    request.split(b"\r\n", 1)[0]
                    == f"PUT /provider/v1/execution-attempts/{ATTEMPT_REF}/progress HTTP/1.1".encode()
                ):
                    self.assertFalse(failed, "The phase PUT must not be resent")
                    self.assertEqual(json.loads(body)["phase"], "preparing")
                    failed = True
                    fixture.peer.response_status = 503
                    fixture.peer.response_headers = response_headers(status=503, request_id=b"request:phase-unconfirmed")
                    return b"PRIVATE_MODEL_KEY private_key=PRIVATE_CREDENTIAL maintenance"
                return result

            fixture.peer.response_factory = response
            process = await fixture.start(child_script="interrupted_model_process.py")
            notice = json.loads(await asyncio.wait_for(process.stderr.readline(), 3))
            self.assertEqual(notice["event"], "api_progress_unconfirmed")
            self.assertEqual(notice["phase"], "preparing")
            self.assertIs(notice["progress_delivery_confirmed"], False)
            self.assertEqual(notice["job_ref"], fixture.ports.instructions.job_ref)
            self.assertEqual(notice["execution_attempt_ref"], ATTEMPT_REF)
            rendered = json.dumps(notice)
            self.assertIn("request:phase-unconfirmed", rendered)
            self.assertIn('"status": 503', rendered)
            self.assertNotIn("PRIVATE", rendered)
            await fixture.observed("model-head-received")
            self.assertEqual(len(model.requests), 1)
            requests = [request.split(b"\r\n", 1)[0] for request in fixture.peer.requests]
            put = requests.index(f"PUT /provider/v1/execution-attempts/{ATTEMPT_REF}/progress HTTP/1.1".encode())
            self.assertEqual(requests[put + 1], f"GET /provider/v1/execution-attempts/{ATTEMPT_REF} HTTP/1.1".encode())
            self.assertIsNone(process.returncode)
            process.send_signal(signal.SIGTERM)
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 1, stderr.decode())
            self.assertNotIn(b"api_progress_recovered", stderr)
            self.assertFalse((fixture.diagnostics / "science-entered").exists())

    async def test_known_keys_equal_valid_references_are_redacted_before_notice_and_failed_output_retention(self):
        for recovery_kind in ("observation", "start", "terminal", "condition", "prediction"):
            for failed_sink in (False, True):
                with self.subTest(recovery_kind=recovery_kind, failed_sink=failed_sink):
                    await self.check_attempt_reference_redaction(recovery_kind=recovery_kind, failed_sink=failed_sink)

    async def check_attempt_reference_redaction(self, *, recovery_kind, failed_sink):
        async with ProcessPeers() as fixture:
            pending = record()
            attempt = acknowledge_start(pending, admission())
            original = fixture.config.interpreter.endpoints[0]
            endpoints = tuple(
                replace(original, spec=replace(original.spec, api_key=key, configuration_id=f"reference-key-{index}"))
                for index, key in enumerate((attempt.admission.job_ref, attempt.admission.execution_attempt_ref))
            )
            config = replace(fixture.config, interpreter=replace(fixture.config.interpreter, endpoints=endpoints))
            outage = ApiFailure("Attempt API exchange", "maintenance")
            if recovery_kind == "prediction":
                outage = PredictionExecutionFailed("Allocator failed; api_key=PRIVATE_CREDENTIAL; " + attempt.admission.job_ref)

            class NoticeService:
                work_task = unconfirmed_hello = None

                def __init__(self, **arguments):
                    if recovery_kind == "prediction":
                        self.policy = arguments["open_attempt"].on_prediction_retry
                    else:
                        self.policy = getattr(arguments["open_attempt"], f"{recovery_kind}_recovery")
                    if recovery_kind in ("start", "terminal") and self.policy.window_seconds != getattr(
                        arguments["config"].lifecycle, f"{recovery_kind}_retry_window_seconds"
                    ):
                        raise AssertionError("Foreground retry window did not reach its configured policy")

                async def run(self, _stop):
                    if recovery_kind == "prediction":
                        self.policy(attempt, outage)
                    elif recovery_kind == "condition":
                        self.policy.on_retry(attempt, "preparing", "waiting_capacity", outage, 0.1)
                        self.policy.on_recovered(attempt, "preparing", "waiting_capacity", outage)
                    else:
                        self.policy.on_retry(pending if recovery_kind == "start" else attempt, outage, 0.1)
                        self.policy.on_recovered(attempt, outage)

                async def settle(self):
                    return None

            def fail_initial_notice(content):
                if json.loads(content)["event"] == "provider_shutdown_observation":
                    return _write_api_notice(content)
                raise OSError(errno.EIO, "Authored sink failure")

            reader, writer = os.pipe()
            try:
                with (
                    os.fdopen(writer, "w") as output,
                    redirect_stderr(output),
                    patch("nmrnet_provider.process.ProviderService", NoticeService),
                    (
                        patch("nmrnet_provider.process._write_api_notice", side_effect=fail_initial_notice)
                        if failed_sink
                        else nullcontext()
                    ),
                ):
                    self.assertEqual(
                        await run_provider(
                            config,
                            execution_identity={},
                            selected_artifacts={},
                            stop=asyncio.Event(),
                            diagnostics=fixture.diagnostics,
                        ),
                        0,
                    )
                content = os.read(reader, 65536)
            finally:
                os.close(reader)
            if failed_sink:
                shutdown = json.loads(content)
                self.assertEqual(shutdown["event"], "provider_shutdown_observation")
                self.assertEqual(shutdown["operation"], "Notice delivery was not confirmed")
                notices = [shutdown["notice"]]
                self.assertIn("OSError", json.dumps(shutdown["output"]))
                self.assertIn(
                    "Allocator failed" if recovery_kind == "prediction" else "Attempt API exchange",
                    json.dumps(shutdown["failure"]),
                )
                self.assertNotIn(attempt.admission.job_ref, content.decode())
                self.assertNotIn(attempt.admission.execution_attempt_ref, content.decode())
                self.assertNotIn("PRIVATE_CREDENTIAL", content.decode())
                self.assertFalse((fixture.diagnostics / "result.json").exists())
            else:
                notices = [json.loads(line) for line in content.splitlines()]
                expected = [f"api_{recovery_kind}_retry", f"api_{recovery_kind}_recovered"]
                if recovery_kind == "prediction":
                    expected = ["prediction_retry_started"]
                self.assertEqual([item["event"] for item in notices], expected)
            for notice in notices:
                self.assertEqual(notice["job_ref"], "[redacted]")
                if notice["event"] == "api_start_retry":
                    self.assertNotIn("execution_attempt_ref", notice)
                else:
                    self.assertEqual(notice["execution_attempt_ref"], "[redacted]")
                rendered = json.dumps(shutdown if failed_sink else notice)
                self.assertNotIn(attempt.admission.job_ref, rendered)
                self.assertNotIn(attempt.admission.execution_attempt_ref, rendered)
                if recovery_kind == "prediction":
                    self.assertIn("Allocator failed", rendered)
                    self.assertNotIn("PRIVATE_CREDENTIAL", rendered)
            self.assertEqual(fixture.peer.requests, [])

    async def test_active_observation_recovery_reaches_the_operator_with_real_job_and_attempt_context(self):
        async with TlsPeer("body-stall") as model, ProcessPeers(model_peer=model) as fixture:
            original = fixture.peer.response_factory
            failed = False

            async def response(request, body):
                nonlocal failed
                fixture.peer.response_status = 200
                fixture.peer.response_headers = response_headers(request_id=b"request:process-proof")
                if (
                    not failed
                    and model.requests
                    and request.split(b"\r\n", 1)[0] == f"GET /provider/v1/execution-attempts/{ATTEMPT_REF} HTTP/1.1".encode()
                ):
                    verify_signed_request(request, body, origin=fixture.peer.origin, key=fixture.config.api.identity.private_key)
                    failed = True
                    fixture.peer.response_status = 503
                    fixture.peer.response_headers = response_headers(status=503, request_id=b"request:active-receiving")
                    return b"PRIVATE_MODEL_KEY private_key=PRIVATE_CREDENTIAL maintenance"
                return await original(request, body)

            fixture.peer.response_factory = response
            process = await fixture.start(child_script="interrupted_model_process.py")
            await fixture.observed("model-head-received")
            retry = json.loads(await asyncio.wait_for(process.stderr.readline(), 3))
            recovered = json.loads(await asyncio.wait_for(process.stderr.readline(), 3))
            self.assertEqual([retry["event"], recovered["event"]], ["api_observation_retry", "api_observation_recovered"])
            for notice in (retry, recovered):
                self.assertEqual(notice["execution_attempt_ref"], ATTEMPT_REF)
                self.assertEqual(notice["job_ref"], fixture.ports.instructions.job_ref)
                rendered = json.dumps(notice)
                self.assertIn("request:active-receiving", rendered)
                self.assertIn('"status": 503', rendered)
                self.assertNotIn("PRIVATE", rendered)
            self.assertEqual(len(model.requests), 1)
            self.assertFalse(model.closed.is_set())
            self.assertIsNone(process.returncode)
            process.send_signal(signal.SIGTERM)
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 1, stderr.decode())
            self.assertFalse((fixture.diagnostics / "science-entered").exists())
            self.assertEqual(len(model.requests), 1)

    def first_inventory_outage(self, fixture):
        """Fail startup's first signed GET before Hello or workers can run."""
        original = fixture.peer.response_factory
        used = False

        async def reply(request, body):
            nonlocal used
            fixture.peer.response_status = 200
            fixture.peer.response_headers = response_headers(request_id=b"request:process-proof")
            if not used and b"/execution-attempts?" in request.split(b"\r\n", 1)[0]:
                verify_signed_request(request, body, origin=fixture.peer.origin, key=fixture.config.api.identity.private_key)
                used = True
                fixture.peer.response_status = 503
                fixture.peer.response_headers = response_headers(status=503, request_id=b"request:startup-outage")
                return b"PRIVATE_MODEL_KEY private_key=PRIVATE_CREDENTIAL maintenance"
            return await original(request, body)

        fixture.peer.response_factory = reply

    async def test_foreground_read_retry_and_recovery_reach_protected_operator_output(self):
        async with ProcessPeers() as fixture:
            self.first_inventory_outage(fixture)
            process = await fixture.start()
            await asyncio.wait_for(fixture.discovered.wait(), 5)
            process.send_signal(signal.SIGTERM)
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            notices = [json.loads(line) for line in stderr.splitlines()]
            self.assertEqual([notice["event"] for notice in notices], ["api_read_retry", "api_read_recovered"])
            for notice in notices:
                rendered = json.dumps(notice)
                self.assertIn('"status": 503', rendered)
                self.assertIn("request:startup-outage", rendered)
                self.assertNotIn("PRIVATE", rendered)
            self.assertGreater(notices[0]["retry_seconds"], 0)
            self.assertFalse((fixture.diagnostics / "failure.json").exists())
            self.assertEqual(await fixture.reopen_store(), ())

    async def test_full_retry_notice_pipe_does_not_prevent_recovery_or_shutdown(self):
        await self.check_full_notice_pipe(fail_after_recovery=False)

    async def test_foreground_hello_recovery_confirms_only_the_new_send_and_clears_shutdown_uncertainty(self):
        async with ProcessPeers() as fixture:
            original = fixture.peer.response_factory
            hello_count = 0

            async def response(request, body):
                nonlocal hello_count
                fixture.peer.response_status = 200
                fixture.peer.response_headers = response_headers()
                if b"POST /provider/v1/hello HTTP/1.1" in request:
                    hello_count += 1
                    if hello_count == 1:
                        fixture.peer.response_status = 503
                        fixture.peer.response_headers = response_headers(status=503, request_id=b"request:prior-hello")
                        return b"PRIVATE_MODEL_KEY private_key=PRIVATE_CREDENTIAL maintenance"
                return await original(request, body)

            fixture.peer.response_factory = response
            process = await fixture.start()
            await asyncio.wait_for(fixture.discovered.wait(), 5)
            process.send_signal(signal.SIGTERM)
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            notices = [json.loads(line) for line in stderr.splitlines()]
            self.assertEqual([item["event"] for item in notices], ["api_hello_retry", "api_hello_recovered"])
            self.assertEqual([item["latest_hello_send_confirmed"] for item in notices], [False, True])
            for item in notices:
                rendered = json.dumps(item)
                self.assertIn("request:prior-hello", rendered)
                self.assertIn('"status": 503', rendered)
                self.assertNotIn("PRIVATE", rendered)
            self.assertFalse((fixture.diagnostics / "result.json").exists())
            self.assertEqual(hello_count, 2)
            self.assertEqual(await fixture.reopen_store(), ())

    async def test_full_retry_notice_pipe_preserves_failure_in_the_owned_error_without_file_fallback(self):
        await self.check_full_notice_pipe(fail_after_recovery=True)

    async def check_full_notice_pipe(self, *, fail_after_recovery):
        """Keep actual stderr full through both normal and failed retirement."""
        async with ProcessPeers() as fixture:
            self.first_inventory_outage(fixture)
            stop = asyncio.Event()
            reader, writer = os.pipe()
            try:
                os.set_blocking(writer, False)
                with self.assertRaises(BlockingIOError):
                    while True:
                        os.write(writer, b"x" * 4096)
                os.set_blocking(writer, True)
                with os.fdopen(writer, "w", closefd=False) as output, redirect_stderr(output):
                    task = asyncio.create_task(
                        run_provider(
                            fixture.config,
                            execution_identity={},
                            selected_artifacts={},
                            stop=stop,
                            diagnostics=fixture.diagnostics,
                        )
                    )
                    try:
                        await asyncio.wait_for(fixture.discovered.wait(), 3)
                        self.assertFalse(task.done())
                        self.assertTrue(os.get_blocking(writer))
                        if fail_after_recovery:
                            fixture.bad_reply = True
                        else:
                            stop.set()
                        if fail_after_recovery:
                            with self.assertRaises(ProviderProcessFailed) as caught:
                                await asyncio.wait_for(task, 3)
                            failed = json.dumps(failure_evidence(caught.exception, secrets=()))
                            self.assertIn("request:startup-outage", failed)
                            self.assertIn("BlockingIOError", failed)
                            self.assertIn('"status": 200', failed)
                            self.assertNotIn("PRIVATE", failed)
                        else:
                            self.assertEqual(await asyncio.wait_for(task, 3), 0)
                    finally:
                        stop.set()
                        await asyncio.gather(task, return_exceptions=True)
            finally:
                os.close(writer)
                os.close(reader)
            self.assertFalse((fixture.diagnostics / "result.json").exists())
            self.assertFalse((fixture.diagnostics / "failure.json").exists())
            self.assertEqual(await fixture.reopen_store(), ())

    def test_oversized_notice_leaves_no_partial_json_before_the_next_record(self):
        reader, writer = os.pipe()
        try:
            os.set_blocking(reader, False)
            with os.fdopen(writer, "w", closefd=False) as output, redirect_stderr(output):
                maximum = os.fpathconf(writer, "PC_PIPE_BUF")
                with self.assertRaises(DiagnosticOutputRefused):
                    _write_api_notice(b"x" * (maximum + 1))
                with self.assertRaises(BlockingIOError):
                    os.read(reader, 1)
                complete = b'{"event":"next-record"}\n'
                _write_api_notice(complete)
                self.assertEqual(os.read(reader, len(complete)), complete)
                self.assertTrue(os.get_blocking(writer))
        finally:
            os.close(writer)
            os.close(reader)

    async def test_sigterm_during_periodic_hello_preserves_unconfirmed_acceptance_without_a_failed_stop(self):
        await self.check_interrupted_hello(after_outage=False)

    async def test_sigterm_during_a_new_hello_preserves_its_head_instead_of_the_prior_outage(self):
        await self.check_interrupted_hello(after_outage=True)

    async def check_interrupted_hello(self, *, after_outage):
        async with ProcessPeers() as fixture:
            fixture.stall_periodic_hello()
            if after_outage:
                original_mode = fixture.peer.response_mode
                original_reply = fixture.peer.response_factory

                def response_mode(request, body):
                    hello_count = sum(b"POST /provider/v1/hello HTTP/1.1" in item for item in fixture.peer.requests)
                    fixture.peer.response_status = 200
                    fixture.peer.response_headers = response_headers(request_id=b"request:latest-hello")
                    if b"POST /provider/v1/hello HTTP/1.1" in request and hello_count == 2:
                        return "success"
                    return original_mode(request, body)

                async def response(request, body):
                    if b"POST /provider/v1/hello HTTP/1.1" in request:
                        hello_count = sum(b"POST /provider/v1/hello HTTP/1.1" in item for item in fixture.peer.requests)
                        if hello_count == 2:
                            fixture.peer.response_status = 503
                            fixture.peer.response_headers = response_headers(status=503, request_id=b"request:prior-hello")
                            return b"PRIVATE maintenance"
                    return await original_reply(request, body)

                fixture.peer.response_mode = response_mode
                fixture.peer.response_factory = response
            process = await fixture.start(child_script="interrupted_model_process.py")
            await asyncio.wait_for(fixture.discovered.wait(), 5)
            await fixture.observed("hello-head-3-received" if after_outage else "hello-head-2-received")
            process.send_signal(signal.SIGTERM)
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            observation = json.loads(stderr.splitlines()[-1])
            self.assertEqual(observation["event"], "provider_shutdown_observation")
            self.assertEqual(observation["operation"], "Latest Hello acceptance was not confirmed")
            rendered = json.dumps(observation)
            self.assertIn('"status": 200', rendered)
            self.assertIn("request:latest-hello" if after_outage else "request:process-proof", rendered)
            self.assertNotIn("request:prior-hello", rendered)
            self.assertIn("request cancelled", rendered)
            self.assertNotIn("PRIVATE", rendered)
            self.assertNotIn(b"PRIVATE", stderr)
            self.assertFalse((fixture.diagnostics / "failure.json").exists())
            self.assertFalse((fixture.diagnostics / "science-entered").exists())
            self.assertEqual(await fixture.reopen_store(), ())
            self.assertEqual(
                sum(request.split(b"\r\n", 1)[0].endswith(b"/hello HTTP/1.1") for request in fixture.peer.requests),
                3 if after_outage else 2,
            )

    async def test_sigterm_during_initial_hello_backoff_reports_uncertainty_without_admission(self):
        async with ProcessPeers() as fixture:
            fixture.files.write(fixture.files.raw.replace(b"idle_seconds = 0.2", b"idle_seconds = 30"))
            original = fixture.peer.response_factory

            async def response(request, body):
                if b"POST /provider/v1/hello HTTP/1.1" in request:
                    verify_signed_request(request, body, origin=fixture.peer.origin, key=fixture.config.api.identity.private_key)
                    fixture.peer.response_status = 503
                    fixture.peer.response_headers = response_headers(status=503, request_id=b"request:initial-hello")
                    return b"PRIVATE_MODEL_KEY private_key=PRIVATE_CREDENTIAL maintenance"
                return await original(request, body)

            fixture.peer.response_factory = response
            process = await fixture.start()
            notice = json.loads(await asyncio.wait_for(process.stderr.readline(), 5))
            self.assertEqual(notice["event"], "api_hello_retry")
            self.assertIs(notice["latest_hello_send_confirmed"], False)
            process.send_signal(signal.SIGTERM)
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            observation = json.loads(stderr.splitlines()[-1])
            self.assertEqual(observation["event"], "provider_shutdown_observation")
            self.assertEqual(observation["operation"], "Latest Hello acceptance was not confirmed")
            rendered = json.dumps(observation)
            self.assertIn('"status": 503', rendered)
            self.assertIn("request:initial-hello", rendered)
            self.assertNotIn("PRIVATE", rendered)
            self.assertFalse((fixture.diagnostics / "admitted").exists())
            self.assertFalse(fixture.discovered.is_set())
            self.assertEqual(len(fixture.peer.requests), 2)
            self.assertEqual(await fixture.reopen_store(), ())

    async def test_required_process_routes_reach_every_http_role_without_changing_owner_settings(self):
        async with ProcessPeers() as fixture:
            original = fixture.config
            first = original.interpreter.endpoints[0]
            second = replace(first, spec=replace(first.spec, configuration_id="secondary"))
            original = replace(original, interpreter=replace(original.interpreter, endpoints=(first, second)))
            routed = route_provider_config(
                original, api="/routes/api.sock", interpreter="/routes/model.sock", store="/routes/store.sock"
            )
            pairs = [
                (original.api.http, routed.api.http, "/routes/api.sock"),
                (original.uploads.http, routed.uploads.http, "/routes/store.sock"),
            ]
            pairs.extend(
                (old.http, new.http, "/routes/model.sock")
                for old, new in zip(original.interpreter.endpoints, routed.interpreter.endpoints, strict=True)
            )
            for old, new, socket_path in pairs:
                self.assertEqual(new.proxy_socket, socket_path)
                for name in ("trust", "exchange_seconds", "retirement_seconds"):
                    self.assertIs(getattr(new, name), getattr(old, name))
                transport = new.new_transport()
                self.assertEqual(transport._proxy_socket, socket_path)
                await transport.retire()
                self.assertIsNone(old.proxy_socket)
            for field in fields(original):
                if field.name not in ("api", "interpreter", "uploads"):
                    self.assertIs(getattr(routed, field.name), getattr(original, field.name))
            self.assertIs(routed.api.identity, original.api.identity)
            self.assertIs(routed.uploads.limits, original.uploads.limits)
            self.assertEqual(routed.interpreter.max_turns, original.interpreter.max_turns)
            for old, new in zip(original.interpreter.endpoints, routed.interpreter.endpoints, strict=True):
                self.assertIs(new.spec, old.spec)
                self.assertEqual((new.request_bytes, new.response_bytes), (old.request_bytes, old.response_bytes))

    async def test_factory_owned_storage_can_settle_before_service_releases_its_dependencies(self):
        async with ProcessPeers() as fixture:
            fixture.files.state.mkdir(mode=0o700)
            config = fixture.config
            store = AttemptStore(
                fixture.files.state, scope=config.lifecycle.scope, limits=config.lifecycle.store_limits, operation_seconds=2
            )
            await store.open()
            transport = config.api.http.new_transport()
            api = ProviderAPI(config.api.identity, transport, expected_topology=config.api.expected_topology)
            factory = NativeAttemptFactory(
                config=config,
                api=api,
                store=store,
                claims=AttemptClaims(),
                execution_identity={},
                selected_artifacts={},
                policy=config.service.native,
                observation_recovery=ObservationRecovery(
                    config.lifecycle.observation_gap_seconds,
                    config.service.policy.api_backoff_max_seconds,
                    lambda _attempt, _failure, _seconds: self.fail("No observation retry expected before admission"),
                    lambda _attempt, _failure: self.fail("No observation recovery expected before admission"),
                ),
                condition_recovery=ConditionRecovery(
                    config.lifecycle.observation_gap_seconds,
                    config.service.policy.api_backoff_max_seconds,
                    lambda _attempt, _phase, _code, _failure, _seconds: self.fail("No condition retry before admission"),
                    lambda _attempt, _phase, _code, _failure: self.fail("No condition recovery before admission"),
                ),
                start_recovery=StartRecovery(
                    config.lifecycle.start_retry_window_seconds,
                    config.service.policy.api_backoff_max_seconds,
                    lambda _pending, _failure, _seconds: self.fail("No START retry expected before admission"),
                    lambda _attempt, _failure: self.fail("No START recovery expected before admission"),
                ),
                on_unconfirmed_phase_progress=lambda _attempt, _phase, _failure: self.fail(
                    "No phase-progress outage expected before admission"
                ),
                on_prediction_retry=lambda _attempt, _failure: self.fail("No prediction retry expected before admission"),
                terminal_recovery=TerminalRecovery(
                    config.lifecycle.terminal_retry_window_seconds,
                    config.service.policy.api_backoff_max_seconds,
                    lambda _attempt, _failure, _seconds: self.fail("No terminal retry expected before admission"),
                    lambda _attempt, _failure: self.fail("No terminal recovery expected before admission"),
                ),
            )
            service = ProviderService(
                config=config,
                store=store,
                api=api,
                open_attempt=factory,
                release_native_history=factory.release_history,
                current_announcement=lambda: config.service.announcement,
                policy=config.service.policy,
                on_started=lambda: None,
                on_api_retry=lambda _kind, _failure, _seconds: self.fail("Unexpected API retry during this storage witness"),
                on_api_recovered=lambda _kind, _failure: self.fail("Unexpected API recovery during this storage witness"),
            )
            entered, release = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()
            read = store._files.records

            def blocked_read():
                loop.call_soon_threadsafe(entered.set)
                release.wait()
                return read()

            try:
                with patch.object(store._files, "records", blocked_read):
                    task = asyncio.create_task(store.records(owner=factory))
                    await asyncio.wait_for(entered.wait(), 2)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    with self.assertRaises(ServiceFailed):
                        await service.run(asyncio.Event())
                    self.assertIs(store.pending_owner, factory)
                    release.set()
                    failures = []
                    self.assertTrue(await _settle_borrowers(service, factory, failures))
                    self.assertEqual([operation for operation, _ in failures], ["service settlement"])
                    self.assertIsNone(store.pending_operation)
                    self.assertEqual(fixture.events, [])
            finally:
                release.set()
                if store.pending_operation is not None:
                    await store.settle(owner=factory)
                await service.settle()
                await transport.retire()
                await store.close()

    async def test_declared_execution_and_selected_members_have_an_explicit_public_projection(self):
        async with ProcessPeers() as fixture:
            models = json.loads(fixture.models.read_bytes())
            # Archive acquisition details are not selected-member identities and
            # are deliberately excluded, matching the qualified native result.
            models["dictionary"]["archive"] = {"PRIVATE_ACQUISITION": "PRIVATE_CANARY"}
            models["checkpoint_archive"]["archive"] = {"PRIVATE_ACQUISITION": "PRIVATE_CANARY"}
            fixture.models.write_text(json.dumps(models))
            execution, selected = _runtime_facts(fixture.execution, fixture.models, fixture.config)
            expected = json.loads(fixture.execution.read_bytes())
            expected.update(
                startup_configuration_sha256=fixture.config.source_sha256,
                prediction_declaration_sha256=hashlib.sha256(fixture.models.read_bytes()).hexdigest(),
                execution_declaration_sha256=hashlib.sha256(fixture.execution.read_bytes()).hexdigest(),
            )
            self.assertEqual(execution, expected)
            self.assertEqual(set(selected), {"dictionary", "elements"})
            for member in (
                selected["dictionary"],
                *(member for pair in selected["elements"].values() for member in pair.values()),
            ):
                self.assertEqual(set(member), {"archive_sha256", "member", "size", "sha256"})
            self.assertNotIn("PRIVATE", json.dumps((execution, selected)))
            self.assertEqual(fixture.peer.requests, [])
            self.assertFalse(fixture.files.state.exists())

    async def test_signed_startup_discovery_and_sigterm_release_the_store(self):
        async with ProcessPeers() as fixture:
            process = await fixture.start()
            await asyncio.wait_for(fixture.discovered.wait(), 5)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(fixture.events[:4], ["execution-attempts", "hello", "execution-attempts", "jobs"])
            self.assertEqual(await fixture.reopen_store(), ())
            self.assertFalse((fixture.diagnostics / "result.json").exists())
            self.assertNotIn(b"PRIVATE", stdout + stderr)


class ProcessDiagnosticTests(unittest.TestCase):
    def test_reviewed_protocol_and_input_causes_remain_actionable_after_cleanup_failure(self):
        for cause in (
            WorkerProtocolError("Scientific reply belongs to another worker allocation"),
            InputRejected("Cannot read coordinates at line 4: x, y and z must be numbers"),
        ):
            failure = PreparationRetirementFailed((("source", OSError(errno.EIO, "PRIVATE cleanup")),), operation_failure=cause)
            report = json.dumps(failure_evidence(failure, secrets=()))
            self.assertIn(str(cause), report)
            self.assertNotIn("PRIVATE", report)

    def test_interpreter_endpoint_index_and_replaced_interruption_cause_remain_visible(self):
        previous = OSError(errno.ECONNRESET, "PRIVATE prior reason")
        cancelled = asyncio.CancelledError("PRIVATE cancellation")
        conversation = ConversationRetirementFailed(((0, cancelled),), cause=cancelled)
        conversation.interruption_cause = previous
        cancelled.__cause__ = conversation
        preparation = PreparationRetirementFailed((("interpreter", cancelled),), operation_failure=conversation)
        preparation.interruption_cause = previous
        report = json.dumps(failure_evidence(preparation, secrets=()))
        self.assertIn("interpreter endpoint 0 cleanup", report)
        self.assertIn('"errno": 104', report)
        self.assertIn("interruption cause", report)
        self.assertNotIn("PRIVATE", report)

    def test_nested_lifecycle_api_and_independent_cleanup_survive_with_correlation_and_effects(self):
        response = HttpResponse(
            503, ((b"x-request-id", b"request:operator-proof"), (b"x-private", b"PRIVATE_HEADER")), b"PRIVATE_BODY"
        )
        api = ApiFailure("read live Attempt inventory", "API returned HTTP 503", response=response)
        api.attempt_mutation_may_have_committed = True
        api.cause = OSError(errno.ECONNRESET, "PRIVATE transport cause")
        api.add_note("PRIVATE_NOTE")
        primary = ExecutionFailed(
            cause=api,
            outcome=None,
            unconfirmed_record=None,
            api_failure=api,
            cleanup_failures=(),
            attempt=acknowledge_start(record(), admission()),
        )
        cleanup = OSError(errno.EIO, "PRIVATE_CLEANUP", "/PRIVATE_PATH")
        controller = ControllerFailed(primary, (cleanup,))
        rendered = json.dumps(failure_evidence(ServiceFailed((("Attempt work", controller),)), secrets=()))
        for useful in (
            "Attempt work",
            "ControllerFailed",
            "ExecutionFailed",
            "ApiFailure",
            "request:operator-proof",
            '"status": 503',
            '"attempt_mutation_may_have_committed": true',
            '"errno": 5',
        ):
            self.assertIn(useful, rendered)
        self.assertNotIn("PRIVATE", rendered)
        self.assertIn('"errno": 104', rendered)
