"""Output tiers preserve usable shifts and reserve only their selected public view."""

from tools.container_guard import require_container

require_container()

import copy
import json
import unittest
from dataclasses import replace

from tests.unit.result_fakes import precision_documents, supplied_documents

from nmrnet_provider.prepared_input import MIN_SHIFT_SIGNIFICANT_DIGITS, MAX_SHIFT_SIGNIFICANT_DIGITS, DEFAULT_SHIFT_SIGNIFICANT_DIGITS

from nmrnet_provider.result import MAX_RESULT_BYTES, ResultCapacityExceeded, ResultPlan
from provider_core.result_verbosity import ResultVerbosity


class ResultVerbosityTests(unittest.TestCase):
    def test_medium_default_keeps_full_coordinates_and_private_audit_identity(self):
        prepared, facts, outcomes = supplied_documents()
        manifest = {"tool.py": {"sha256": "a" * 64, "size": 123}}
        facts["execution_identity"].update(tool_sources=manifest, runtime_identity_source="caller")
        prepared = replace(prepared, interpretation=b'{"outcome":"prepared"}')
        plan = ResultPlan(prepared, **facts)
        public = json.loads(plan.finish(outcomes))
        retained = json.loads(plan.prepared_identity)
        self.assertEqual("nmrnet.coordinate_shifts.result.v2", public["schema_id"])
        self.assertEqual({"verbosity": "medium", "shift_significant_digits": MAX_SHIFT_SIGNIFICANT_DIGITS}, public["output"])
        self.assertNotIn("tool_sources", public["execution"])
        self.assertEqual({"outcome": "prepared"}, public["interpretation"])
        self.assertEqual("caller", public["execution"]["runtime_identity_source"])
        self.assertEqual(retained["execution"]["tool_sources"], manifest)
        self.assertEqual({"outcome": "prepared"}, retained["interpretation"])
        self.assertEqual(retained["output"], public["output"])
        self.assertIn("prepared_request", public["geometries"][0])
        self.assertIn("float32_coordinates", public["geometries"][0])

    def test_every_tier_keeps_scientific_values_source_mapping_and_private_evidence(self):
        for route in ("supplied", "generated"):
            for digits in (MIN_SHIFT_SIGNIFICANT_DIGITS, DEFAULT_SHIFT_SIGNIFICANT_DIGITS, MAX_SHIFT_SIGNIFICANT_DIGITS):
                with self.subTest(route=route, digits=digits):
                    prepared, facts, outcomes = (
                        supplied_documents()
                        if route == "supplied"
                        else precision_documents((32.309967041015625, 3.062568426132202, 0.001234567, -1.23456789, -0.0))
                    )
                    manifest = {"tool.py": {"sha256": "a" * 64, "size": 123}}
                    facts["execution_identity"]["tool_sources"] = manifest
                    prepared = replace(prepared, interpretation=b'{"outcome":"prepared"}')
                    outcomes[0]["private_warnings"] = [
                        {
                            "category": "sklearn.exceptions.InconsistentVersionWarning",
                            "message": "PRIVATE_WARNING_SENTINEL",
                            "filename": "/PRIVATE_WARNING_PATH",
                            "lineno": 1,
                            "head": "H",
                            "versions": {"original": "1.5.2", "current": "1.8.0"},
                        }
                    ]
                    original = copy.deepcopy(outcomes)
                    documents = {}
                    evidence = []
                    for verbosity in ResultVerbosity:
                        output = replace(prepared.output, verbosity=verbosity, shift_significant_digits=digits)
                        plan = ResultPlan(replace(prepared, output=output), **facts)
                        documents[verbosity] = json.loads(plan.finish(outcomes))
                        evidence.append(plan.scientific_evidence_reservation())
                        self.assertLessEqual(len(plan.finish(outcomes)), plan.maximum_result_bytes)
                        self.assertEqual(json.loads(plan.prepared_identity)["execution"]["tool_sources"], manifest)
                        self.assertEqual("scaler_version_mismatch", documents[verbosity]["warnings"][0]["code"])
                        self.assertNotIn("PRIVATE_WARNING", json.dumps(documents[verbosity]))
                    self.assertEqual(outcomes, original)
                    self.assertEqual(evidence[0], evidence[1])
                    self.assertEqual(evidence[1], evidence[2])
                    maximum = documents[ResultVerbosity.MAX]
                    for verbosity, document in documents.items():
                        expected = copy.deepcopy(maximum)
                        expected["output"]["verbosity"] = verbosity.value
                        if verbosity is not ResultVerbosity.MAX:
                            del expected["execution"]["tool_sources"]
                        if verbosity is ResultVerbosity.LOW:
                            for geometry in expected["geometries"]:
                                del geometry["prepared_request"]
                                del geometry["float32_coordinates"]
                        self.assertEqual(document, expected)
                    for geometry, full_geometry in zip(
                        documents[ResultVerbosity.LOW]["geometries"], maximum["geometries"], strict=True
                    ):
                        self.assertEqual(geometry["atoms"], full_geometry["prepared_request"]["atoms"])
                        self.assertEqual(geometry["source"], full_geometry["source"])
                    if route == "generated":
                        provenance = documents[ResultVerbosity.LOW]["generation"]["provenance"]
                        self.assertEqual([[1, 0], [2, 0], [3, 0], [4, 0]], provenance["added_hydrogen_parents"])
                        self.assertEqual(0, provenance["source_atoms"][0]["source_atom_index"])

    def test_public_reservation_is_selected_separately_from_private_identity_admission(self):
        """This isolates public sizing; session tests separately refuse oversized private identity."""
        prepared, facts, outcomes = supplied_documents()
        facts["execution_identity"]["tool_sources"] = {
            f"tools/module_{index}.py": {"sha256": "a" * 64, "size": index} for index in range(MAX_RESULT_BYTES // 80)
        }
        medium = ResultPlan(prepared, **facts)
        self.assertGreater(len(medium.prepared_identity), MAX_RESULT_BYTES)
        self.assertLess(medium.maximum_result_bytes, MAX_RESULT_BYTES)
        self.assertLessEqual(len(medium.finish(outcomes)), medium.maximum_result_bytes)
        with self.assertRaises(ResultCapacityExceeded):
            ResultPlan(replace(prepared, output=replace(prepared.output, verbosity=ResultVerbosity.MAX)), **facts)
