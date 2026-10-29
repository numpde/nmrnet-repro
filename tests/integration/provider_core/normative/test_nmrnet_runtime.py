"""Actual configured assembly joins signed discovery to each interpreted route."""

import asyncio
import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from tests.fakes.execution import unexpected_condition
from tests.fakes.attempts import start_response
from tests.fakes.job_input import facts, response
from tests.fakes.uploads import UPLOAD_REF, member
from tests.integration.nmrnet.interpreter_ports import UploadPorts, tool_reply
from tests.integration.nmrnet.pasted_ports import PastedPorts
from tests.integration.nmrnet.runtime_ports import RuntimePeers
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.runtime import assemble_attempt
from provider_core.attempt_record import AcknowledgedAttempt
from provider_core.controller import StartDeferred
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from provider_core.job_input import verify_job_input
from provider_core.uploads import UploadInputRejected


class ConfiguredRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_late_start_ack_after_admission_stop_does_not_enter_science(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            admission, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
            original_reply = fixture.api_peer.response_factory

            async def reply(request, body):
                if request.split(b"\r\n", 1)[0].split()[1].endswith(b"/start"):
                    entered.set()
                    await release.wait()
                return await original_reply(request, body)

            fixture.api_peer.response_factory = reply
            controller = await assemble_attempt(**(fixture.arguments(pending) | {"admission_stopping": admission}))
            fixture.stack.push_async_callback(fixture.settle_controller, controller)
            task = asyncio.create_task(controller.run(pending))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                admission.set()
                release.set()
                outcome = await asyncio.wait_for(task, 3)
                self.assertIs(type(outcome), StartDeferred)
                self.assertTrue(outcome.may_have_committed)
                self.assertEqual(fixture.model_peer.requests, [])
                self.assertEqual(ports.worker.events, [])
                self.assertEqual(sum(b"/start HTTP/1.1" in request for request in fixture.api_peer.requests), 1)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_stopped_admission_never_starts_or_enters_science(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            admission = asyncio.Event()
            controller = await assemble_attempt(**(fixture.arguments(pending) | {"admission_stopping": admission}))
            fixture.stack.push_async_callback(fixture.settle_controller, controller)
            admission.set()
            outcome = await controller.run(pending)
            self.assertIs(type(outcome), StartDeferred)
            self.assertFalse(outcome.may_have_committed)
            self.assertFalse(any(b"/start HTTP/1.1" in request for request in fixture.api_peer.requests))
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertEqual(ports.worker.events, [])

    async def test_terminal_start_replay_never_acquires_prediction_or_interprets_input(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            controller = await assemble_attempt(**fixture.arguments(pending))
            fixture.stack.push_async_callback(fixture.settle_controller, controller)
            with patch("tests.integration.nmrnet.runtime_ports.start_response", return_value=start_response(
                analysis_kind_ref=pending.analysis_kind_ref, state="failed", replayed=True,
            )):
                outcome = await controller.run(pending)
            self.assertIsInstance(outcome, AcknowledgedAttempt)
            fixture.allocate_prediction.assert_not_awaited()
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertEqual(ports.worker.events, [])
            ports.allocate_generator.assert_not_awaited()
            ports.allocate_reader.assert_not_awaited()

    async def test_prediction_replacement_keeps_one_interpretation_and_one_terminal_publication(self):
        ports = SessionPorts()
        replacement = type(ports.worker)(ports.worker.artifacts, ports.worker.runtime)
        host = type(ports.host)()
        first = asyncio.IncompleteReadError(b"PRIVATE incomplete response", 4)
        ports.worker.execute = AsyncMock(side_effect=first)
        notices = Mock()
        acquired = []

        async def allocate():
            if not acquired:
                acquired.append(ports.worker)
                return await ports.allocate_prediction()
            self.assertTrue(ports.host.confirmed)
            self.assertEqual(ports.worker.events[-2:], ["close", "closed"])
            return replacement, host, replacement.runtime

        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            arguments = fixture.arguments(pending) | {"allocate_prediction": allocate, "on_prediction_retry": notices}
            controller = await assemble_attempt(**arguments)
            fixture.stack.push_async_callback(fixture.settle_controller, controller)
            outcome = await controller.run(pending)
            self.assertIs(type(outcome), DeliveryAcceptedCheckpoint)
            await fixture.wait_published()
            self.assertEqual(len(fixture.model_peer.requests), 1)
            ports.allocate_generator.assert_awaited_once()
            ports.generator.generate.assert_awaited_once()
            self.assertEqual(replacement.requests, ports.worker.requests)
            self.assertEqual(replacement.events, ["prepare", "execute", "close", "closed"])
            self.assertTrue(host.confirmed)
            self.assertIs(notices.call_args.args[1], first)
            requests = [request.split(b"\r\n", 1)[0] for request in fixture.api_peer.requests]
            self.assertEqual(sum(b"/start HTTP/1.1" in request for request in requests), 1)
            self.assertEqual(sum(b"/complete HTTP/1.1" in request for request in requests), 1)
            self.assertEqual(len(fixture.result["geometries"]), 2)

    async def test_unit_correction_uses_the_declared_job_unit_before_one_reader_and_prediction(self):
        source = b"H 0.125 0 0\n"
        ports = PastedPorts(source, source_format="table", source_unit="nm")
        instructions = ports.instructions.specification_bytes + b" The coordinates are explicitly in nm."
        ports.instructions = verify_job_input(response(instructions), feed=facts(instructions))
        async with RuntimePeers(ports, "pasted") as fixture:
            fixture.config = replace(fixture.config, interpreter=replace(fixture.config.interpreter, max_turns=2))

            async def model_reply(request, raw):
                self.assertIn(b"authorization: bearer private_model_key\r\n", request.lower())
                self.assertNotIn(b"signature:", request.lower())
                messages = json.loads(raw)["messages"]
                self.assertEqual(messages[1]["content"], instructions.decode())
                corrected = messages[-1]["role"] == "tool"
                if corrected:
                    self.assertEqual(
                        json.loads(messages[-1]["content"]),
                        {"rejected": "Cannot prepare coordinates: specify source unit angstrom, nm or bohr."},
                    )
                    ports.allocate_reader.assert_not_awaited()
                    ports.allocate_generator.assert_not_awaited()
                    ports.uploads.acquire.assert_not_awaited()
                    self.assertEqual(ports.worker.events, [])
                return tool_reply(
                    "corrected" if corrected else "invalid",
                    "prepare_pasted",
                    {"source_text": source.decode(), "source_format": "table", "source_unit": "nm" if corrected else "unknown"},
                )

            fixture.model_peer.response_factory = model_reply
            pending = await fixture.discover()
            controller = await assemble_attempt(**fixture.arguments(pending))
            fixture.stack.push_async_callback(fixture.settle_controller, controller)
            prepare = ports.worker.prepare
            durable = []
            execute_session = controller.session.execute
            selected_identity = None

            async def capture_identity(handle, **kwargs):
                nonlocal selected_identity
                selected_identity = handle.identity
                return await execute_session(handle, **kwargs)

            async def require_frozen_selection(requests):
                self.assertTrue(fixture.claims.owns_job(pending.scope, pending.input.job_ref))
                identity = json.loads(selected_identity.content)
                self.assertEqual(identity["geometries"][0]["source"]["input_fingerprint"], pending.input.input_fingerprint)
                durable.append(identity)
                return await prepare(requests)

            with (
                patch.object(controller.session, "execute", side_effect=capture_identity),
                patch.object(ports.worker, "prepare", side_effect=require_frozen_selection),
            ):
                outcome = await controller.run(pending)
            self.assertIsInstance(outcome, DeliveryAcceptedCheckpoint)
            await fixture.wait_published()
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
            self.assertEqual(len(fixture.model_peer.requests), 2)
            self.assertEqual(fixture.result["interpretation"], durable[0]["interpretation"])
            self.assertEqual(fixture.result["output"]["verbosity"], "medium")
            self.assertEqual(durable[0]["interpretation"]["turns_started"], 2)
            self.assertEqual(durable[0]["interpretation"]["corrections"], 1)
            self.assertEqual(durable[0]["interpretation"]["operations"], ["prepare_pasted"])
            (geometry,) = fixture.result["geometries"]
            self.assertEqual(
                geometry["prepared_request"], {"atoms": ["H"], "coordinates": [[1.25, 0, 0]], "coordinate_unit": "angstrom"}
            )
            self.assertEqual(geometry["preparation"]["source_unit"], "nm")
            self.assertEqual(geometry["preparation"]["scale_to_angstrom"], 10)
            self.assertEqual(geometry["source"]["sha256"], hashlib.sha256(source).hexdigest())
            self.assertEqual(geometry["source"]["positions"], [1])
            self.assertEqual(geometry["source"]["input_fingerprint"], pending.input.input_fingerprint)
            self.assertEqual(durable[0]["geometries"][0]["source"], geometry["source"])
            self.assertEqual(durable[0]["geometries"][0]["request_sha256"], hashlib.sha256(ports.worker.requests[0]).hexdigest())
            self.assertEqual(
                durable[0]["interpretation"], json.loads(selected_identity.content)["interpretation"]
            )
            ports.allocate_reader.assert_awaited_once()
            ports.reader.read.assert_awaited_once_with(source, source_format="table", source_unit="nm", selection=None)
            ports.allocate_generator.assert_not_awaited()
            self.assertEqual(ports.worker.events, ["prepare", "execute", "close", "closed"])
            self.assertTrue(ports.host.confirmed)

    async def test_selected_precision_and_exact_ram_evidence_need_no_local_checkpoint_storage(self):
        ports = SessionPorts()
        execute = ports.worker.execute

        async def long_predictions(handle):
            outcomes = await execute(handle)
            for item in outcomes:
                for row in item["prediction"]["predictions"]:
                    row["shift_ppm"] = 32.309967041015625
            return outcomes

        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            controller = await assemble_attempt(**fixture.arguments(pending))
            fixture.stack.push_async_callback(fixture.settle_controller, controller)
            execute_session = controller.session.execute
            captured = None

            async def capture_outcome(handle, **kwargs):
                nonlocal captured
                captured = await execute_session(handle, **kwargs)
                return captured

            with (
                patch.object(ports.worker, "execute", side_effect=long_predictions),
                patch.object(controller.session, "execute", side_effect=capture_outcome),
            ):
                outcome = await controller.run(pending)
            self.assertIsInstance(outcome, DeliveryAcceptedCheckpoint)
            await fixture.wait_published()
            self.assertEqual(fixture.result["output"], {"shift_significant_digits": 4, "verbosity": "max"})
            for geometry in fixture.result["geometries"]:
                self.assertTrue(all(row["shift_ppm"] == 32.31 for row in geometry["predictions"]))
            evidence = json.loads(captured.evidence.content)
            for geometry in evidence["geometries"]:
                self.assertTrue(all(row["shift_ppm"] == 32.309967041015625 for row in geometry["prediction"]["predictions"]))
            body, raw_evidence = fixture.api_peer.request_bodies[-1], captured.evidence.content
            self.assertEqual(hashlib.sha256(body).hexdigest(), outcome.selection.body_sha256.removeprefix("sha256:"))
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())

            alternative = SessionPorts()

            async def choose(_instructions, capability):
                await capability.prepare_smiles(smiles="CCl", requested_count=2, output={"shift_significant_digits": 2})

            session = alternative.make_session(alternative.owner(choose))
            fixture.stack.push_async_callback(session.retire)
            handle = await session.prepare(alternative.acknowledged, alternative.instructions, report_condition=unexpected_condition)
            self.assertEqual(json.loads(handle.identity.content)["output"], {"shift_significant_digits": 2, "verbosity": "medium"})
            self.assertEqual(fixture.api_peer.request_bodies[-1], body)
            self.assertEqual(captured.evidence.content, raw_evidence)
            self.assertEqual(alternative.worker.events, [])
            self.assertEqual(len(fixture.model_peer.requests), 1)
            self.assertEqual(ports.worker.events, ["prepare", "execute", "close", "closed"])

    async def test_upload_selection_refusals_correct_in_the_same_conversation_before_one_grant(self):
        for mistake in ("not_inspected", "unknown_member", "changed_membership", "oversized_member"):
            with self.subTest(mistake=mistake):
                await self.check_mistake(mistake)

    async def check_mistake(self, mistake):
        ports = UploadPorts()
        async with RuntimePeers(ports, "upload") as fixture:
            settings = fixture.config.uploads
            fixture.config = replace(
                fixture.config, uploads=replace(settings, limits=replace(settings.limits, per_source_bytes=len(ports.source)))
            )
            original_api = fixture.api_reply
            membership_reads = 0

            async def api_reply(request, body):
                nonlocal membership_reads
                response = await original_api(request, body)
                if request.split(b"\r\n", 1)[0].endswith(b"/uploads HTTP/1.1"):
                    membership_reads += 1
                    if mistake == "changed_membership" and membership_reads >= 2:
                        document = json.loads(response)
                        document["uploads"][0]["description"] = "Changed description"
                        response = json.dumps(document).encode()
                    elif mistake == "oversized_member":
                        document = json.loads(response)
                        document["uploads"].append(
                            member(upload_ref="upload:sha256:" + "f" * 64, byte_length=len(ports.source) + 1)
                        )
                        response = json.dumps(document).encode()
                return response

            async def model_reply(_headers, raw):
                messages = json.loads(raw)["messages"]
                replies = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
                corrected = any("rejected" in reply for reply in replies)
                if replies and "rejected" in replies[-1]:
                    acquisition = controller.session._input_preparation._uploads
                    self.assertIsInstance(acquisition.last_failure, UploadInputRejected)
                    self.assertFalse(acquisition.last_failure.grant_issued)
                    self.assertEqual(acquisition.received_bytes, 0)
                    ports.allocate_reader.assert_not_awaited()
                    ports.allocate_generator.assert_not_awaited()
                    self.assertEqual(fixture.store_peer.requests, [])
                    self.assertFalse(any(b"/read-capability " in request for request in fixture.api_peer.requests))
                    return tool_reply("reinspect", "inspect_uploads", {})
                if not replies and mistake != "not_inspected":
                    return tool_reply("inspect", "inspect_uploads", {})
                upload_ref = (
                    "upload:sha256:" + "f" * 64
                    if mistake in ("unknown_member", "oversized_member") and not corrected
                    else UPLOAD_REF
                )
                return tool_reply(
                    f"prepare-{len(replies)}",
                    "prepare_upload",
                    {
                        "upload_ref": upload_ref,
                        "source_format": "xyz",
                        "source_unit": "angstrom",
                        "selection": [2, 1],
                        "alternate_location": None,
                    },
                )

            fixture.api_peer.response_factory = api_reply
            fixture.model_peer.response_factory = model_reply
            pending = await fixture.discover()
            controller = await assemble_attempt(**fixture.arguments(pending))
            fixture.stack.push_async_callback(fixture.settle_controller, controller)
            execute_session = controller.session.execute
            selected = None

            async def capture_identity(handle, **kwargs):
                nonlocal selected
                selected = handle.identity
                return await execute_session(handle, **kwargs)

            with patch.object(controller.session, "execute", side_effect=capture_identity):
                confirmed = await controller.run(pending)
            self.assertIsInstance(confirmed, DeliveryAcceptedCheckpoint)
            await fixture.wait_published()
            self.assertEqual(fixture.result["interpretation"]["corrections"], 1)
            self.assertEqual(fixture.result["interpretation"], json.loads(selected.content)["interpretation"])
            self.assertEqual(len(fixture.model_peer.requests), 3 if mistake == "not_inspected" else 4)
            self.assertEqual(len(fixture.store_peer.requests), 1)
            self.assertEqual(sum(b"/read-capability " in request for request in fixture.api_peer.requests), 1)
            ports.allocate_reader.assert_awaited_once()
            self.assertEqual(ports.worker.events, ["prepare", "execute", "close", "closed"])

    async def test_returned_controller_never_acquires_prediction_when_not_run(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            controller = await assemble_attempt(**fixture.arguments(pending))
            await controller.settle()
            self.assertFalse(ports.host.confirmed)
            self.assertEqual(ports.worker.events, [])
            fixture.allocate_prediction.assert_not_awaited()
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertEqual(len(fixture.api_peer.requests), 1)
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())

    async def test_three_routes_consume_discovered_identity_and_publish_a_complete_result(self):
        for route in ("smiles", "pasted", "upload"):
            with self.subTest(route=route):
                if route == "smiles":
                    ports = SessionPorts()
                elif route == "pasted":
                    ports = PastedPorts(b"H 0.125 0 0\n", source_format="table", source_unit="nm")
                else:
                    ports = UploadPorts()
                async with RuntimePeers(ports, route) as fixture:
                    pending = await fixture.discover()
                    fixture.verify_discovery_signature()
                    controller = await assemble_attempt(**fixture.arguments(pending))
                    fixture.stack.push_async_callback(fixture.settle_controller, controller)
                    self.assertEqual(ports.worker.events, [])
                    ports.allocate_generator.assert_not_awaited()
                    ports.allocate_reader.assert_not_awaited()
                    outcome = await controller.run(pending)
                    self.assertIsInstance(outcome, DeliveryAcceptedCheckpoint)
                    await fixture.wait_published()
                    self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
                    self.assertTrue(ports.host.confirmed)
                    self.assertEqual(ports.worker.events, ["prepare", "execute", "close", "closed"])
                    self.assertEqual(fixture.result["input_fingerprint"], pending.input.input_fingerprint)
                    self.assertIn("interpretation", fixture.result)
                    if route == "pasted":
                        self.assertEqual(fixture.result["geometries"][0]["prepared_request"]["coordinates"], [[1.25, 0, 0]])
                    if route == "upload":
                        self.assertEqual([item["source"]["record_number"] for item in fixture.result["geometries"]], [2, 1])
                        self.assertEqual(len(fixture.store_peer.requests), 1)
                        self.assertNotIn(b"signature:", fixture.store_peer.requests[0].lower())
