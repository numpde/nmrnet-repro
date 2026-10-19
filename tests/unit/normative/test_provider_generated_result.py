"""Generated provenance and ordered predictions use the existing result owner."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import sys
import unittest

from tests.unit.result_fakes import scientific_facts, scientific_outcomes
from tests.unit.smiles_fakes import collection

from nmrnet_provider.result import ResultPlan


class GeneratedResultTests(unittest.TestCase):
    def setUp(self):
        self.batch = collection()
        self.facts = scientific_facts()

    def test_generated_origin_and_stereo_remain_distinct_from_file_positions(self):
        plan = ResultPlan(self.batch, **self.facts)
        document = json.loads(plan.finish(scientific_outcomes(self.batch, self.facts)))
        self.assertEqual(json.loads(self.batch.provenance), document["generation"]["provenance"])
        self.assertEqual(hashlib.sha256(self.batch.provenance).hexdigest(), document["generation"]["provenance_sha256"])
        for ordinal, (geometry, conformer) in enumerate(zip(document["geometries"], self.batch.conformers, strict=True), 1):
            self.assertEqual(geometry["geometry_id"], f"geometry-{ordinal}")
            self.assertEqual(
                geometry["source"],
                {
                    "kind": "smiles",
                    "sha256": document["generation"]["provenance"]["source_sha256"],
                    "conformer_id": conformer.conformer_id,
                },
            )
            self.assertEqual(json.loads(conformer.request_bytes), geometry["prepared_request"])
            self.assertEqual(json.loads(conformer.stereochemistry), geometry["preparation"]["stereochemistry"])
            self.assertEqual(geometry["preparation"]["coordinate_unit"], "angstrom")
            self.assertNotIn("scale_to_angstrom", geometry["preparation"])
        self.assertNotIn(b"PRIVATE_NATIVE_DIAGNOSTIC", plan.finish(scientific_outcomes(self.batch, self.facts)))
        self.assertNotIn("rdkit", sys.modules)
        self.assertNotIn("provider_core", sys.modules)

    def test_identity_binds_generation_and_stereo_without_coordinates(self):
        plan = ResultPlan(self.batch, **self.facts)
        identity = json.loads(plan.prepared_identity)
        self.assertEqual(identity["generation"], {"provenance_sha256": hashlib.sha256(self.batch.provenance).hexdigest()})
        self.assertEqual([item["source"]["conformer_id"] for item in identity["geometries"]], [2, 0])
        for geometry, conformer in zip(identity["geometries"], self.batch.conformers, strict=True):
            self.assertEqual(geometry["request_sha256"], hashlib.sha256(conformer.request_bytes).hexdigest())
            self.assertEqual(
                geometry["preparation"]["stereochemistry_sha256"], hashlib.sha256(conformer.stereochemistry).hexdigest()
            )
        for field in (b'"coordinates"', b'"prepared_request"', b'"stereochemistry"', b'"source_atoms"'):
            self.assertNotIn(field, plan.prepared_identity)

    def test_generated_coverage_and_limits_describe_graph_expansion_without_averaging(self):
        plan = ResultPlan(self.batch, **self.facts)
        content = plan.finish(scientific_outcomes(self.batch, self.facts))
        document = json.loads(content)
        for ordinal, geometry in enumerate(document["geometries"], 1):
            self.assertEqual(
                geometry["coverage"],
                {
                    "basis": "smiles_graph_expansion",
                    "source_explicit_hydrogen_atoms": 0,
                    "added_hydrogen_atoms": 4,
                    "coordinate_hydrogen_atoms": 4,
                    "predicted_hydrogen_atoms": 4,
                    "hydrogen_completeness": "not_independently_validated",
                },
            )
            self.assertEqual([item["shift_ppm"] for item in geometry["predictions"]], [float(ordinal)] * 5)
        limits = " ".join(document["scientific_limits"])
        self.assertIn("not experimental", limits)
        self.assertIn("without checkpoint or conformer averaging", limits)
        self.assertIn("not model input features", limits)
        self.assertNotIn("implicit hydrogens are not added", limits)
        self.assertLessEqual(len(content), plan.maximum_result_bytes)
