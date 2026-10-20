"""Actual configured assembly joins signed discovery to each interpreted route."""

import json
import unittest
from dataclasses import replace

from tests.fakes.uploads import UPLOAD_REF, member
from tests.integration.nmrnet.interpreter_ports import UploadPorts, tool_reply
from tests.integration.nmrnet.pasted_ports import PastedPorts
from tests.integration.nmrnet.runtime_ports import RuntimePeers
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.runtime import assemble_attempt
from provider_core.attempt_record import TerminalConfirmed
from provider_core.uploads import UploadInputRejected


class ConfiguredRuntimeTests(unittest.IsolatedAsyncioTestCase):
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
                        document["uploads"].append(member(upload_ref="upload:sha256:" + "f" * 64, byte_length=len(ports.source) + 1))
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
            fixture.stack.push_async_callback(controller.settle)
            confirmed = await controller.run(pending)
            self.assertIsInstance(confirmed, TerminalConfirmed)
            self.assertEqual(fixture.result["interpretation"]["corrections"], 1)
            self.assertEqual(len(fixture.model_peer.requests), 3 if mistake == "not_inspected" else 4)
            self.assertEqual(len(fixture.store_peer.requests), 1)
            self.assertEqual(sum(b"/read-capability " in request for request in fixture.api_peer.requests), 1)
            ports.allocate_reader.assert_awaited_once()
            self.assertEqual(ports.worker.events, ["prepare", "execute", "close", "closed"])

    async def test_returned_controller_retires_its_allocation_even_when_never_run(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            controller = await assemble_attempt(**fixture.arguments(pending))
            await controller.settle()
            self.assertTrue(ports.host.confirmed)
            self.assertEqual(ports.worker.events, ["close", "closed"])
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertEqual(len(fixture.api_peer.requests), 1)
            self.assertEqual(await fixture.store.records(), ())

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
                    fixture.stack.push_async_callback(controller.settle)
                    self.assertEqual(ports.worker.events, [])
                    ports.allocate_generator.assert_not_awaited()
                    ports.allocate_reader.assert_not_awaited()
                    outcome = await controller.run(pending)
                    self.assertIsInstance(outcome, TerminalConfirmed)
                    self.assertEqual(await fixture.store.records(), (outcome,))
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
