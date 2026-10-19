"""Generated inputs use the same one-shot prediction binding as supplied inputs.

These are recording-port checks; native generation/model accuracy and real host
retirement have separate qualification lanes.
"""

import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.prepared_input import PreparedJobInput
from nmrnet_provider.xyz import prepare_xyz


class SmilesSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_batch_keeps_order_and_diagnostics_through_one_prediction_path(self):
        ports = SessionPorts()
        self.addAsyncCleanup(ports.session.retire)
        handle = await ports.session.prepare(ports.acknowledged, ports.instructions)
        self.assertEqual(ports.worker.events, [])
        ports.generator_host.retire.assert_awaited_once()
        identity = json.loads(handle.identity.content)
        requests = tuple(item.request_bytes for item in ports.batch.conformers)
        self.assertEqual(
            [item["request_sha256"] for item in identity["geometries"]],
            [hashlib.sha256(raw).hexdigest() for raw in requests],
        )
        diagnostics = identity["preparation_diagnostics"]
        self.assertEqual(diagnostics[0]["diagnostic"], ports.batch.diagnostic)
        self.assertEqual(diagnostics[0]["provenance_sha256"], hashlib.sha256(ports.batch.provenance).hexdigest())
        self.assertNotIn("coordinates", json.dumps(identity))
        self.assertNotIn("SMILES CCl", json.dumps(identity))
        result = await ports.session.execute(handle)
        self.assertEqual(ports.worker.requests, requests)
        self.assertEqual(ports.worker.events, ["prepare", "execute"])
        public = json.loads(result.content)
        self.assertEqual(public["generation"]["provenance"]["added_hydrogen_parents"], [[2, 0], [3, 0], [4, 0]])
        self.assertEqual([item["source"]["conformer_id"] for item in public["geometries"]], [2, 0])
        for ordinal, geometry in enumerate(public["geometries"], 1):
            self.assertEqual([row["atom_index"] for row in geometry["predictions"]], [0, 2, 3, 4])
            self.assertEqual([row["shift_ppm"] for row in geometry["predictions"]], [float(ordinal)] * 4)
            self.assertEqual(geometry["coverage"]["source_explicit_hydrogen_atoms"], 0)
            for count in ("added_hydrogen_atoms", "coordinate_hydrogen_atoms", "predicted_hydrogen_atoms"):
                self.assertEqual(geometry["coverage"][count], 3)
            self.assertEqual(geometry["coverage"]["hydrogen_completeness"], "not_independently_validated")
            self.assertEqual(
                geometry["preparation"]["stereochemistry"],
                {"origin": "generated_coordinates", "method": "authored fixture", "atoms": [], "bonds": []},
            )
        self.assertEqual(json.loads(result.evidence.content)["preparation_diagnostics"], diagnostics)
        self.assertNotIn(b"PRIVATE_", result.content)
        ports.generator.generate.assert_awaited_once_with(b"CCl", 2)
        with self.assertRaises(ValueError):
            await ports.session.execute(handle)

    async def test_supplied_reader_diagnostics_keep_geometry_correspondence_privately(self):
        ports = SessionPorts()
        source = b"1\nAuthored first, angstrom\nH 0 0 0\n1\nAuthored second, angstrom\nH 1 2 3\n"
        second, first = prepare_xyz(source, source_unit="angstrom", selection=(2, 1))
        geometries = (
            replace(second, diagnostic="PRIVATE_SECOND"),
            replace(first, diagnostic="PRIVATE_FIRST"),
        )
        preparation = AsyncMock()
        preparation.prepare.return_value = PreparedJobInput(geometries, "xyz")
        ports.session = ports.make_session(preparation)
        self.addAsyncCleanup(ports.session.retire)
        handle = await ports.session.prepare(ports.acknowledged, ports.instructions)
        outcome = await ports.session.execute(handle)
        diagnostics = json.loads(outcome.evidence.content)["preparation_diagnostics"]
        self.assertEqual([row["diagnostic"] for row in diagnostics], ["PRIVATE_SECOND", "PRIVATE_FIRST"])
        self.assertEqual([row["geometry_id"] for row in diagnostics], ["geometry-1", "geometry-2"])
        self.assertEqual([row["source_record_number"] for row in diagnostics], [2, 1])
        self.assertEqual([row["source_sha256"] for row in diagnostics], [hashlib.sha256(source).hexdigest()] * 2)
        self.assertEqual(json.loads(handle.identity.content)["preparation_diagnostics"], diagnostics)
        self.assertNotIn(b"PRIVATE_", outcome.content)
