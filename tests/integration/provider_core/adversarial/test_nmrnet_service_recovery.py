"""Live exclusions and delivery duties survive waits; restart grants no action."""

import asyncio
import json
import unittest
from dataclasses import asdict, replace
from urllib.parse import urlsplit
from unittest.mock import AsyncMock, patch

from tests.fakes.attempts import start_response
from tests.fakes.api import response_headers
from tests.fakes.api_tls import verify_signed_request
from tests.fakes.job_feed import page
from tests.integration.nmrnet.interpreter_ports import tool_reply
from tests.integration.nmrnet.runtime_ports import RuntimePeers
from tests.integration.nmrnet.service_ports import ServicePeers, SuccessiveServicePeers
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.runtime import assemble_attempt
from nmrnet_provider.config import ANALYSIS_KIND_REF
from nmrnet_provider.input_geometry import prepare_json
from nmrnet_provider.prepared_input import PreparedJobInput
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.service import ServiceFailed
from provider_core.attempt_record import StartPending
from provider_core.attempt_request import start_body
from provider_core.controller import LiveStartPolicy, StartDeferred
from provider_core.delivery_client import DeliveryUnavailable
from provider_core.delivery_store import DeliveryFull
from provider_core.execution import ExecutionRefused
from provider_core.producer_checkpoint import SuppressedCheckpoint


class LiveServiceOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_settled_source_outage_holds_its_job_and_allows_another(self):
        async with SuccessiveServicePeers(maximum_live_attempts=2, lose_second_completion=False) as fixture:
            first, second = fixture.jobs[:2]
            source_requests, failure_bodies = [], []
            first_owner = []
            original_api = fixture.api_peer.response_factory

            def scientific_ports(pending):
                index = next(i for i, job in enumerate(fixture.jobs) if job.identity == pending.input)
                ports = SessionPorts()
                ports.instructions = fixture.jobs[index]
                ports.uploads.job_ref = ports.instructions.job_ref
                return ports

            async def model_reply(request, raw):
                if fixture.ports.instructions.job_ref == first.job_ref:
                    return tool_reply("inspect", "inspect_uploads", {})
                return await RuntimePeers.model_reply(fixture, request, raw)

            async def api_reply(request, body):
                fixture.api_peer.response_status = 200
                fixture.api_peer.response_headers = response_headers()
                method, target, _ = request.split(b"\r\n", 1)[0].decode().split()
                path = urlsplit(target).path
                verify_signed_request(request, body, origin=fixture.api_peer.origin,
                                      key=fixture.config.api.identity.private_key)
                if path == "/provider/v1/jobs":
                    jobs = [first] if not fixture.start_bodies else [first, second]
                    return json.dumps(page() | {
                        "analysis_kind_ref": ANALYSIS_KIND_REF, "next_cursor": None,
                        "jobs": [{**asdict(job.identity), "analysis_kind_ref": ANALYSIS_KIND_REF,
                                  "created_at": "2026-10-18T00:00:00.123456Z"} for job in jobs],
                    }).encode()
                if path == "/provider/v1/execution-attempts/start":
                    index = len(fixture.start_bodies)
                    self.assertLess(index, 2)
                    self.assertEqual(json.loads(body)["job_ref"], fixture.jobs[index].job_ref)
                    if index == 1:
                        self.assertTrue(fixture.claims.owns_job(fixture.config.lifecycle.scope, first.job_ref))
                        first_owner.extend(owner for owner in fixture.claims.live_owners() if owner is not service.controller)
                    fixture.start_bodies.append(body)
                    return start_response(execution_attempt_ref=fixture.attempt_refs[index],
                                          job_ref=fixture.jobs[index].job_ref, analysis_kind_ref=ANALYSIS_KIND_REF)
                if path == f"/provider/v1/jobs/{first.job_ref}/uploads":
                    source_requests.append(path)
                    fixture.api_peer.response_status = 503
                    fixture.api_peer.response_headers = response_headers(status=503, request_id=b"request:source-outage")
                    return b"Source temporarily unavailable"
                if method == "POST" and path.endswith("/fail"):
                    failure_bodies.append(body)
                result = await original_api(request, body)
                if method == "POST" and path.endswith("/complete"):
                    fixture.stop.set()
                return result

            fixture.scientific_ports = scientific_ports
            fixture.model_peer.response_factory = model_reply
            fixture.api_peer.response_factory = api_reply
            service = fixture.service()
            fixture.stack.push_async_callback(fixture.settle_service, service)
            await asyncio.wait_for(service.run(fixture.stop), 8)
            await fixture.wait_published()
            self.assertEqual(len(source_requests), 3)
            self.assertEqual(len(fixture.start_bodies), 2)
            self.assertTrue(fixture.claims.owns_job(fixture.config.lifecycle.scope, first.job_ref))
            self.assertEqual(len(first_owner), 1)
            self.assertTrue(first_owner[0].shutdown_ready)
            self.assertEqual(fixture.allocations[0].worker.events, [])
            self.assertEqual(fixture.allocations[1].worker.events.count("execute"), 1)
            self.assertEqual(len(fixture.complete_bodies), 1)
            self.assertEqual(failure_bodies, [])
            notices = [notice for notice in fixture.api_notices if notice[0] == "source_exhausted"]
            self.assertEqual(len(notices), 1)
            failure = notices[0][1]
            exhaustion = failure.cause.cause
            self.assertEqual(type(exhaustion).__name__, "UploadRecoveryExhausted")
            self.assertEqual(exhaustion.cause.status, 503)
            self.assertEqual(exhaustion.cause.request_id, "request:source-outage")
            evidence = json.dumps(failure_evidence(failure, secrets=("PRIVATE_MODEL_KEY",)))
            self.assertIn("UploadRecoveryExhausted", evidence)
            self.assertIn('"status": 503', evidence)
            self.assertIn("request:source-outage", evidence)

    async def test_grouped_settlement_failure_does_not_skip_another_live_controller(self):
        async with ServicePeers() as fixture:
            service = fixture.service()
            failure = BaseExceptionGroup("first controller", [SystemExit(7)])
            # Controller identity indexes require hashable owners.
            class Owner:
                shutdown_ready = False
            owners = [Owner(), Owner()]
            owners[0].settle = AsyncMock(side_effect=failure)
            owners[1].settle = AsyncMock()
            with patch.object(service, "_owned_controllers", return_value=owners):
                with self.assertRaises(BaseExceptionGroup) as caught:
                    await service._settle_controller(retry=False)
            self.assertIs(caught.exception.exceptions[0], failure)
            owners[1].settle.assert_awaited_once()
            self.assertIn(owners[0], service._settlement_tasks)
            self.assertNotIn(owners[1], service._settlement_tasks)

    async def test_stop_observes_inflight_start_past_drain(self):
        for terminal in (True, False):
            async with ServicePeers() as fixture:
                entered, release, elapsed = asyncio.Event(), asyncio.Event(), asyncio.Event()
                original_reply = fixture.api_peer.response_factory
                starts = []

                async def reply(request, body):
                    if request.startswith(b"POST /provider/v1/execution-attempts/start "):
                        starts.append(body)
                        entered.set()
                        await release.wait()
                    return await original_reply(request, body)

                fixture.api_peer.response_factory = reply
                service = fixture.service(drain_seconds=0.02)
                wait_task = service._wait_task

                async def observed_wait(task, seconds):
                    finished = await wait_task(task, seconds)
                    if task is service.work_task and seconds == service.policy.drain_seconds:
                        elapsed.set()
                    return finished

                response = (start_response(analysis_kind_ref=ANALYSIS_KIND_REF, state="failed", replayed=True)
                            if terminal else start_response(analysis_kind_ref=ANALYSIS_KIND_REF))
                with patch.object(service, "_wait_task", observed_wait), patch(
                    "tests.integration.nmrnet.runtime_ports.start_response", return_value=response,
                ):
                    task = asyncio.create_task(service.run(fixture.stop))
                    try:
                        await asyncio.wait_for(entered.wait(), 2)
                        work = service.work_task
                        fixture.stop.set()
                        await asyncio.wait_for(elapsed.wait(), 1)
                        self.assertFalse(work.done())
                        self.assertEqual(work.cancelling(), 0)
                        release.set()
                        await asyncio.wait_for(task, 3)
                        self.assertTrue(service.shutdown_ready)
                        self.assertEqual(
                            fixture.claims.owns_job(fixture.config.lifecycle.scope, fixture.ports.instructions.job_ref),
                            not terminal,
                        )
                        self.assertEqual(len(starts), 1)
                        self.assertEqual(fixture.model_peer.requests, [])
                        self.assertEqual(fixture.allocations[0].worker.events, [])
                    finally:
                        fixture.stop.set()
                        release.set()
                        await asyncio.gather(task, return_exceptions=True)
                        await fixture.settle_service(service)

    async def test_result_size_failure_reaches_api_without_prediction(self):
        async with ServicePeers() as fixture:
            raw = json.dumps({"coordinate_unit": "angstrom", "atoms": ["H"] * 128, "coordinates": [[0, 0, 0]] * 128}).encode()
            geometry = prepare_json(raw)
            original_open = fixture.open_attempt
            original_reply = fixture.api_peer.response_factory
            failures = []

            async def open_attempt(pending, *, stopping):
                controller = await original_open(pending, stopping=stopping)
                controller.session._input_preparation.prepare = AsyncMock(
                    return_value=PreparedJobInput((geometry,) * 128, "direct_coordinate_json")
                )
                return controller

            async def reply(request, body):
                response = await original_reply(request, body)
                if request.split(b"\r\n", 1)[0].split()[1].endswith(b"/fail"):
                    failures.append(json.loads(body))
                    fixture.stop.set()
                return response

            fixture.api_peer.response_factory = reply
            service = fixture.service(open_attempt=open_attempt)
            fixture.stack.push_async_callback(fixture.settle_service, service)
            await asyncio.wait_for(service.run(fixture.stop), 5)
            await fixture.wait_published()
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["failure_code"], "result_size_limit")
            self.assertIn("did not run", failures[0]["failure_message"])
            self.assertIn("may exceed", failures[0]["failure_message"])
            self.assertEqual(fixture.allocations[0].worker.events, ["close", "closed"])
            self.assertTrue(service.shutdown_ready)

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
                await asyncio.wait_for(service.run(fixture.stop), 2)
            await fixture.settle_service(service)
            self.assertEqual(len(starts), 1)
            self.assertTrue(fixture.attempt_stopping.is_set())
            self.assertTrue(service.shutdown_ready)
            self.assertTrue(fixture.claims.owns_job(fixture.config.lifecycle.scope, fixture.ports.instructions.job_ref))
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertEqual(fixture.allocations[0].worker.events, [])

    async def test_fresh_science_does_not_query_prior_attempt_history(self):
        async with ServicePeers() as fixture:
            service = fixture.service()
            fixture.stack.push_async_callback(fixture.settle_service, service)
            await asyncio.wait_for(service.run(fixture.stop), 4)
            await fixture.wait_published()
            self.assertTrue(service.shutdown_ready)
            self.assertEqual(len(fixture.allocations), 1)
            self.assertEqual(fixture.allocations[0].worker.events.count("execute"), 1)
            self.assertNotIn("/provider/v1/execution-attempts", fixture.request_paths)
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


    async def test_selected_report_retries_original_bytes_after_stop_and_drain_expiry(self):
        async with ServicePeers() as fixture:
            controllers, bodies = [], []
            pressured, recovered, window_elapsed = asyncio.Event(), asyncio.Event(), asyncio.Event()
            open_original = fixture.open_attempt

            async def open_attempt(pending, *, stopping):
                controller = await open_original(pending, stopping=stopping)
                controllers.append(controller)
                submit = controller.delivery.submit

                async def bounded_delivery(outcome):
                    bodies.append(outcome.body)
                    if not recovered.is_set():
                        pressured.set()
                        raise DeliveryFull('Authored full queue; retain the selected report')
                    return await submit(outcome)

                controller.delivery.submit = bounded_delivery
                return controller

            service = fixture.service(open_attempt=open_attempt, drain_seconds=0.02)
            wait_task = service._wait_task

            async def observed_wait(task, seconds):
                finished = await wait_task(task, seconds)
                if task is service.work_task and seconds == service.policy.drain_seconds:
                    window_elapsed.set()
                return finished

            with patch.object(service, '_wait_task', observed_wait):
                task = asyncio.create_task(service.run(fixture.stop))
                try:
                    await asyncio.wait_for(pressured.wait(), 3)
                    driver = service.work_task
                    (controller,) = controllers
                    fixture.stop.set()
                    await asyncio.wait_for(window_elapsed.wait(), 1)
                    self.assertFalse(driver.done())
                    self.assertEqual(driver.cancelling(), 0)
                    self.assertFalse(controller.shutdown_ready)
                    with self.assertRaises(ExecutionRefused):
                        await controller.operation.retain_suppressed_disposition()
                    recovered.set()
                    await asyncio.wait_for(asyncio.shield(task), 3)
                    await fixture.wait_published()
                    self.assertIs(service.work_task, driver)
                    self.assertTrue(service.shutdown_ready)
                    self.assertIsNone(service.last_failure)
                    self.assertEqual(fixture.claims.live_owners(), ())
                    self.assertGreaterEqual(len(bodies), 2)
                    self.assertTrue(all(body == bodies[0] for body in bodies))
                    self.assertEqual(len(controllers), 1)
                    self.assertEqual(fixture.allocations[0].worker.events.count('execute'), 1)
                finally:
                    recovered.set()
                    fixture.stop.set()
                    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 4)
                    if service.shutdown_ready:
                        await fixture.settle_service(service)

    async def test_returned_unselected_outcome_requires_supported_explicit_suppression(self):
        async with ServicePeers() as fixture:
            controllers, submissions = [], []
            open_original = fixture.open_attempt

            async def open_attempt(pending, *, stopping):
                controller = await open_original(pending, stopping=stopping)
                controllers.append(controller)
                execute, submit = controller.session.execute, controller.delivery.submit

                async def stop_after_return(*args, **kwargs):
                    result = await execute(*args, **kwargs)
                    stopping.set()
                    return result

                async def observe_submission(outcome):
                    submissions.append(outcome.body)
                    return await submit(outcome)

                controller.session.execute = stop_after_return
                controller.delivery.submit = observe_submission
                return controller

            service = fixture.service(open_attempt=open_attempt)
            with self.assertRaises(ServiceFailed):
                await asyncio.wait_for(service.run(fixture.stop), 3)
            (controller,) = controllers
            self.assertEqual(submissions, [])
            self.assertIsNone(fixture.result)
            self.assertFalse(service.shutdown_ready)
            self.assertTrue(controller.work_settled)
            disposition = await controller.operation.retain_suppressed_disposition()
            self.assertIsInstance(disposition, SuppressedCheckpoint)
            await controller.settle()
            await fixture.settle_service(service)
            self.assertTrue(service.shutdown_ready)
            self.assertEqual(submissions, [])
            self.assertEqual(fixture.allocations[0].worker.events.count('execute'), 1)
