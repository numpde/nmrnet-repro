"""Generated provenance and ordered predictions use the existing result owner."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import subprocess
import sys
import unittest

from tests.unit.result_fakes import generated_job_input, scientific_facts, scientific_outcomes
from tests.unit.smiles_fakes import collection

from nmrnet_provider.result import ResultPlan


class GeneratedResultTests(unittest.TestCase):
    def setUp(self):
        self.batch = collection()
        self.facts = scientific_facts()
        self.prepared = generated_job_input(self.batch)

    def test_generated_origin_and_stereo_remain_distinct_from_file_positions(self):
        plan = ResultPlan(self.prepared, **self.facts)
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
        # The shared vocabulary is pure; result planning must not pull in API
        # transports, storage, cryptography or native scientific dependencies.
        import_probe = """
import json
import sys
import nmrnet_provider.result

modules = sorted(
    name for name in sys.modules
    if name == 'provider_core' or name.startswith('provider_core.')
)
print(json.dumps(modules))
"""
        loaded = subprocess.check_output(
            [sys.executable, "-B", "-c", import_probe],
            text=True,
        )
        self.assertEqual(json.loads(loaded), ["provider_core", "provider_core.container_guard", "provider_core.result_verbosity"])

    def test_identity_binds_generation_and_stereo_without_coordinates(self):
        plan = ResultPlan(self.prepared, **self.facts)
        identity = json.loads(plan.prepared_identity)
        self.assertEqual(identity["generation"]["provenance_sha256"], hashlib.sha256(self.batch.provenance).hexdigest())
        public = json.loads(plan.finish(scientific_outcomes(self.batch, self.facts)))
        self.assertEqual(public["generation"]["selection"], identity["generation"]["selection"])
        self.assertEqual(
            identity["generation"]["selection"],
            {
                "job_ref": "job:fixture",
                "input_fingerprint": self.facts["input_fingerprint"],
                "origin": "interpreter_supplied_smiles",
                "byte_length": 1,
                "requested_count": 2,
                "source_sha256": hashlib.sha256(b"C").hexdigest(),
            },
        )
        self.assertEqual([item["source"]["conformer_id"] for item in identity["geometries"]], [2, 0])
        for geometry, conformer in zip(identity["geometries"], self.batch.conformers, strict=True):
            self.assertEqual(geometry["request_sha256"], hashlib.sha256(conformer.request_bytes).hexdigest())
            self.assertEqual(
                geometry["preparation"]["stereochemistry_sha256"], hashlib.sha256(conformer.stereochemistry).hexdigest()
            )
        for field in (b'"coordinates"', b'"prepared_request"', b'"stereochemistry"', b'"source_atoms"'):
            self.assertNotIn(field, plan.prepared_identity)

    def test_generated_coverage_and_limits_describe_graph_expansion_without_averaging(self):
        plan = ResultPlan(self.prepared, **self.facts)
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
        self.assertIn("If the SMILES contains disconnected components", limits)
        self.assertIn("RDKit embeds them separately", limits)
        self.assertIn("may overlap", limits)
        self.assertIn("relative placement is not physically established", limits)
        self.assertIn("uses all components together", limits)
        self.assertIn("this placement can affect their shifts", limits)
        self.assertNotIn("implicit hydrogens are not added", limits)
        self.assertLessEqual(len(content), plan.maximum_result_bytes)
