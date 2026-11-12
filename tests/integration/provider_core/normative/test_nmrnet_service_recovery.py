"""Successive science clears compact control while the real relay owns delivery."""

import asyncio
import base64
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from tests.fakes.api import response_headers
from tests.fakes.api_tls import verify_signed_request
from tests.integration.nmrnet.admission_ports import feed_response
from tests.integration.nmrnet.budget_service import InputCallLimitPeers, InputTimeLimitPeers
from tests.integration.nmrnet.service_ports import ServicePeers, SuccessiveServicePeers

from nmrnet_provider.input_preparation import InputSelectionTimeLimit, InputWorkerCallLimit
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.service import ServiceFailed
from provider_core.controller import ControllerFailed, LiveStartPolicy
from provider_core.execution import ExecutionFailed


class SuccessiveServiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def finish(self, fixture, service):
        fixture.stack.push_async_callback(fixture.settle_service, service)
        try:
            await asyncio.wait_for(service.run(fixture.stop), 8)
        except ServiceFailed as cause:
            raise AssertionError(json.dumps(failure_evidence(cause, secrets=("PRIVATE_MODEL_KEY",)))) from cause
        await fixture.settle_service(service)
        await fixture.wait_published(3)
        self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
        self.assertEqual(
            [],
            fixture.relay.queue.inventory(fixture.config.lifecycle.scope.api_origin, fixture.config.lifecycle.scope.provider_ref),
        )
        self.assertEqual(3, len(fixture.start_bodies))
        self.assertEqual(3, len(fixture.allocations))
        fixture.require_retired_science(3)

    async def test_unconfirmed_phase_labels_do_not_stop_later_jobs_or_repeat_science(self):
        async with SuccessiveServicePeers(maximum_live_attempts=1) as fixture:
            original_reply = fixture.api_peer.response_factory
            failed = set()

            async def response(request, body):
                fixture.api_peer.response_status = 200
                fixture.api_peer.response_headers = response_headers()
                result = await original_reply(request, body)
                method, target, _ = request.split(b"\r\n", 1)[0].decode().split()
                if method == "PUT" and target.endswith("/progress"):
                    phase = json.loads(body)["phase"]
                    for index, expected in ((0, "preparing"), (1, "running")):
                        if target.endswith(f"/{fixture.attempt_refs[index]}/progress") and phase == expected:
                            self.assertNotIn((index, phase), failed)
                            failed.add((index, phase))
                            fixture.api_peer.response_status = 503
                            fixture.api_peer.response_headers = response_headers(status=503, request_id=b"request:phase")
                            return b"PRIVATE progress response"
                return result

            fixture.api_peer.response_factory = response
            await self.finish(fixture, fixture.service())
            self.assertEqual(failed, {(0, "preparing"), (1, "running")})
            self.assertEqual(
                [
                    (attempt.admission.execution_attempt_ref, phase, failure.status)
                    for attempt, phase, failure in fixture.progress_notices
                ],
                [(fixture.attempt_refs[0], "preparing", 503), (fixture.attempt_refs[1], "running", 503)],
            )
            self.assertEqual(fixture.model_jobs, [job.job_ref for job in fixture.jobs])
            self.assertEqual(4, len(fixture.complete_bodies))
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())

    async def test_time_limited_first_job_retains_attempt_without_starting_later_jobs(self):
        async with InputTimeLimitPeers() as fixture:
            failure = await self.check_preparation_failure(fixture, InputSelectionTimeLimit)
            self.assertEqual(fixture.selection_seconds, failure.seconds)
            self.assertIsInstance(failure.cause, TimeoutError)
            (controller,) = fixture.claims.live_owners()
            diagnostics = json.dumps(json.loads(controller.session._input_preparation.interpreter_diagnostics))
            self.assertIn("request_cancelled", diagnostics)
            self.assertIn("may have been processed", diagnostics)
            self.assertNotIn("PRIVATE_MODEL_KEY", diagnostics)
            self.assertEqual(fixture.model_jobs, [fixture.jobs[0].job_ref])

    async def test_lost_accepted_start_reply_recovers_same_job_before_one_interpretation_and_prediction(self):
        async with ServicePeers() as fixture:
            sent, notices = [], []
            accepted = None
            original_arguments = fixture.arguments
            policy = LiveStartPolicy(
                0.01,
                lambda pending, failure, seconds: notices.append(("retry", pending, failure, seconds)),
                lambda attempt, failure: notices.append(("recovered", attempt, failure)),
            )

            def arguments(pending):
                return original_arguments(pending) | {"start_policy": policy}

            async def response(request, body):
                nonlocal accepted
                if request.split(b"\r\n", 1)[0] == b"POST /provider/v1/execution-attempts/start HTTP/1.1":
                    verify_signed_request(
                        request, body, origin=fixture.api_peer.origin, key=fixture.config.api.identity.private_key
                    )
                    sent.append((request, body))
                    if len(sent) == 1:
                        accepted = json.loads(await fixture.api_reply(request, body))
                        return None
                    self.assertEqual(body, sent[0][1])
                    return json.dumps(accepted | {"replayed": True}).encode()
                return await fixture.api_reply(request, body)

            fixture.api_peer.response_factory = response
            service = fixture.service()
            fixture.stack.push_async_callback(fixture.settle_service, service)
            with patch.object(fixture, "arguments", side_effect=arguments):
                await asyncio.wait_for(service.run(fixture.stop), 5)
            await fixture.settle_service(service)
            await fixture.wait_published()
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
            self.assertEqual(2, len(sent))
            self.assertEqual(sent[0][1], sent[1][1])
            self.assertNotEqual(sent[0][0], sent[1][0])
            self.assertEqual(1, len(fixture.model_peer.requests))
            self.assertEqual(1, len(fixture.allocations))
            self.assertEqual(1, fixture.events.count("start"))
            self.assertEqual(["prepare", "execute", "close", "closed"], fixture.allocations[0].worker.events)
            self.assertEqual([notice[0] for notice in notices], ["retry", "recovered"])
            self.assertTrue(notices[1][1].admission.replayed)
            self.assertEqual(notices[1][1].start.body, sent[0][1])
            self.assertEqual(notices[0][1], notices[1][1].start)
            self.assertIs(notices[0][2], notices[1][2])
            self.assertTrue(notices[0][2].attempt_mutation_may_have_committed)

    async def test_call_limited_first_job_retains_attempt_without_starting_later_jobs(self):
        async with InputCallLimitPeers() as fixture:
            failure = await self.check_preparation_failure(fixture, InputWorkerCallLimit)
            (controller,) = fixture.claims.live_owners()
            self.assertIs(failure, controller.session._input_preparation.chooser_failure)
            self.assertEqual((1, 1), (failure.maximum_calls, failure.calls_started))
            diagnostics = json.dumps(json.loads(controller.session._input_preparation.interpreter_diagnostics))
            self.assertIn("Cannot read coordinate table: x must be a finite number.", diagnostics)
            self.assertNotIn("PRIVATE_MODEL_KEY", diagnostics)
            self.assertEqual(fixture.model_jobs, [fixture.jobs[0].job_ref] * 2)

    async def check_preparation_failure(self, fixture, expected_type):
        service = fixture.service()
        fixture.stack.push_async_callback(fixture.settle_service, service)
        with self.assertRaises(ServiceFailed) as caught:
            await asyncio.wait_for(service.run(fixture.stop), 8)
        (controller,) = fixture.claims.live_owners()
        self.assertIs(caught.exception.cause, controller.last_failure)
        self.assertIs(type(controller.last_failure), ControllerFailed)
        execution = controller.last_failure.cause
        self.assertIs(type(execution), ExecutionFailed)
        self.assertIs(type(execution.cause), expected_type)
        self.assertTrue(controller.shutdown_ready)
        self.assertTrue(fixture.claims.owns_job(fixture.config.lifecycle.scope, fixture.jobs[0].job_ref))
        self.assertEqual(1, len(fixture.start_bodies))
        self.assertEqual(1, len(fixture.allocations))
        fixture.require_retired_science(1)
        self.assertEqual([], fixture.complete_bodies)
        requests = [request.split(b"\r\n", 1)[0] for request in fixture.api_peer.requests]
        self.assertFalse(any(b"/fail HTTP/" in request or b"/complete HTTP/" in request for request in requests))
        self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
        return execution.cause

    async def test_three_jobs_share_one_active_slot_and_relay_recovers_lost_receipt_without_repeating_science(self):
        async with SuccessiveServicePeers(maximum_live_attempts=1) as fixture:
            await self.finish(fixture, fixture.service())
            for index, verbosity in enumerate(("low", "medium", "max")):
                body = fixture.accepted_outcomes[index]
                public = json.loads(base64.b64decode(json.loads(body)["canonical_result_base64"], validate=True))
                self.assertEqual("nmrnet.coordinate_shifts.result.v2", public["schema_id"])
                self.assertEqual(public["output"]["verbosity"], verbosity)
                identity = json.loads(fixture.allocations[index].preparation_outcome.identity.content)
                self.assertEqual(identity["output"], public["output"])
            requests = [ports.worker.requests for ports in fixture.allocations]
            self.assertEqual(requests, [requests[0]] * 3)
            self.assertEqual(fixture.model_jobs, [job.job_ref for job in fixture.jobs])
            self.assertEqual(fixture.complete_bodies, [fixture.accepted_outcomes[index] for index in (0, 1, 1, 2)])
            retry = [(error, facts) for error, facts in fixture.delivery_notices if facts["event"] == "delivery_retry"]
            self.assertEqual(1, len(retry))
            self.assertEqual(retry[0][1]["execution_attempt_ref"], fixture.attempt_refs[1])
            self.assertTrue(retry[0][0].attempt_mutation_may_have_committed)
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())

    async def test_two_jobs_then_producer_restart_keeps_original_relay_delivery_and_runs_only_third_science(self):
        async with SuccessiveServicePeers(maximum_live_attempts=1) as fixture:
            original_reply = fixture.api_peer.response_factory
            third_allowed = False
            fixture.recovery_release = asyncio.Event()

            async def response(request, body):
                target = request.split(b"\r\n", 1)[0].split(b" ")[1].decode()
                if urlsplit(target).path == "/provider/v1/jobs" and len(fixture.start_bodies) == 2 and not third_allowed:
                    return feed_response(next_cursor=None)
                return await original_reply(request, body)

            fixture.api_peer.response_factory = response
            first = fixture.service()
            fixture.stack.push_async_callback(fixture.settle_service, first)
            task = asyncio.create_task(first.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.recovery_entered.wait(), 4)
                fixture.stop.set()
                await asyncio.wait_for(task, 2)
                await fixture.settle_service(first)
                self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
                self.assertEqual(2, len(fixture.allocations))
                fixture.require_retired_science(2)
                retained = fixture.relay.queue.next_pending_handoff(
                    fixture.config.lifecycle.scope.api_origin, fixture.config.lifecycle.scope.provider_ref
                )
                self.assertEqual(retained.outcome.body, fixture.accepted_outcomes[1])
                self.assertEqual(retained.publication, fixture.config.publication)
                fixture.stop = asyncio.Event()
                fixture.attempt_stopping = asyncio.Event()
                third_allowed = True
                fixture.recovery_release.set()
                replacement = fixture.service()
                await self.finish(fixture, replacement)
                self.assertEqual(fixture.model_jobs, [job.job_ref for job in fixture.jobs])
                self.assertEqual(fixture.complete_bodies, [fixture.accepted_outcomes[index] for index in (0, 1, 1, 2)])
                self.assertEqual(2, len(fixture.hellos))
            finally:
                fixture.stop.set()
                fixture.recovery_release.set()
                await asyncio.gather(task, return_exceptions=True)
