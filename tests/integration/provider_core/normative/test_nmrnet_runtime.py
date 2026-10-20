"""Actual configured assembly joins signed discovery to each interpreted route."""

import unittest

from tests.integration.nmrnet.interpreter_ports import UploadPorts
from tests.integration.nmrnet.pasted_ports import PastedPorts
from tests.integration.nmrnet.runtime_ports import RuntimePeers
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.runtime import assemble_attempt
from provider_core.attempt_record import TerminalConfirmed


class ConfiguredRuntimeTests(unittest.IsolatedAsyncioTestCase):
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
