"""Scientific result admission preserves inputs and rejects substituted outcomes."""

from tools.container_guard import require_container

require_container()

import copy
import hashlib
import json
import unittest

from nmrnet_provider.input_geometry import prepare_json
from nmrnet_provider.result import MAX_RESULT_BYTES, ResultPlan, ResultRejected


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.raw = b'{"coordinate_unit":"angstrom","atoms":["C","H","O"],"coordinates":[[0,0,0],[1.09,0,0],[0,1.4,0]]}'
        self.geometry = prepare_json(self.raw)
        member = {"archive_sha256": "a" * 64, "member": "fixture", "size": 1, "sha256": "b" * 64}
        self.artifacts = {
            "dictionary": member,
            "elements": {head: {"checkpoint": member, "scaler": member} for head in ("H", "C")},
        }
        self.runtime = {
            "python": "3.12.12",
            "installed_distributions": {"torch": "2.8.0", "numpy": "2.3.5", "scikit-learn": "1.8.0", "joblib": "1.5.3"},
        }
        self.execution = {"image_id": "sha256:" + "c" * 64, "source_revision": "d" * 40}
        self.plan = self.make_plan()
        self.result = {
            "request_sha256": hashlib.sha256(self.raw).hexdigest(),
            "prediction": {
                "unit": "ppm",
                "predictions": [
                    {"atom_index": 0, "element": "C", "shift_ppm": 42.5},
                    {"atom_index": 1, "element": "H", "shift_ppm": 1.2},
                ],
            },
            "provenance": {"artifacts": copy.deepcopy(self.artifacts), "runtime": copy.deepcopy(self.runtime)},
            "private_warnings": [],
        }

    def make_plan(self):
        return ResultPlan(
            self.geometry,
            preparation_method="direct_coordinate_json",
            job_ref="job:fixture",
            execution_attempt_ref="execution_attempt:sha256:" + "e" * 64,
            input_fingerprint="sha256:" + hashlib.sha256(self.raw).hexdigest(),
            execution_identity=self.execution,
            selected_artifacts=self.artifacts,
            runtime_expectations=self.runtime,
        )

    def test_result_preserves_prepared_and_float32_coordinates_and_source_mapping(self):
        result = json.loads(self.plan.finish(self.result))
        self.assertEqual(result["schema_id"], "nmrnet.coordinate_shifts.result.v1")
        geometry = result["geometries"][0]
        self.assertEqual(geometry["prepared_request"], json.loads(self.raw))
        self.assertNotEqual(geometry["float32_coordinates"][1][0], 1.09)
        self.assertEqual(geometry["source"]["positions"], [0, 1, 2])
        self.assertEqual(geometry["predictions"], self.result["prediction"]["predictions"])
        self.assertEqual(geometry["request_sha256"], hashlib.sha256(self.raw).hexdigest())

    def test_plan_is_not_changed_by_later_caller_mutation(self):
        self.execution["image_id"] = "substituted"
        self.artifacts["dictionary"]["sha256"] = "substituted"
        self.runtime["python"] = "substituted"
        result = json.loads(self.plan.finish(self.result))
        self.assertEqual(result["execution"]["image_id"], "sha256:" + "c" * 64)
        self.assertEqual(result["runtime"]["python"], "3.12.12")

    def test_missing_duplicate_reordered_wrong_element_or_nonfinite_shifts_fail(self):
        rows = self.result["prediction"]["predictions"]
        for predictions in (
            rows[:1],
            rows + rows[:1],
            list(reversed(rows)),
            [rows[0], {**rows[1], "element": "C"}],
            [rows[0], {**rows[1], "atom_index": True}],
            [rows[0], {**rows[1], "shift_ppm": True}],
            [rows[0], {**rows[1], "shift_ppm": float("nan")}],
            [rows[0], {**rows[1], "shift_ppm": float("inf")}],
            [rows[0], {**rows[1], "shift_ppm": 10**1000}],
            [rows[0], {**rows[1], "shift_ppm": 2**53 + 1}],
            [rows[0], {**rows[1], "extra": "private-canary"}],
        ):
            with self.subTest(predictions=predictions):
                result = copy.deepcopy(self.result)
                result["prediction"]["predictions"] = predictions
                with self.assertRaises(ResultRejected) as raised:
                    self.plan.finish(result)
                self.assertNotIn("private-canary", str(raised.exception))

    def test_substituted_request_artifacts_runtime_or_unit_fail(self):
        for path, value in (
            (("request_sha256",), "f" * 64),
            (("provenance", "runtime", "python"), "substituted"),
            (("provenance", "artifacts", "dictionary", "sha256"), "f" * 64),
            (("provenance", "artifacts", "dictionary", "size"), True),
            (("provenance", "runtime", "python"), True),
            (("prediction", "unit"), "Hz"),
        ):
            with self.subTest(path=path):
                result = copy.deepcopy(self.result)
                target = result
                for name in path[:-1]:
                    target = target[name]
                target[path[-1]] = value
                with self.assertRaises(ResultRejected):
                    self.plan.finish(result)

    def test_raw_warning_text_and_paths_never_enter_public_result(self):
        self.result["private_warnings"] = [
            {
                "category": "sklearn.exceptions.InconsistentVersionWarning",
                "message": "private-canary",
                "filename": "/private-canary",
                "lineno": 17,
            }
        ]
        content = self.plan.finish(self.result)
        self.assertNotIn(b"private-canary", content)
        self.assertEqual(json.loads(content)["warnings"][0]["code"], "scaler_version_mismatch")
        self.result["private_warnings"][0]["category"] = "unknown.private-canary"
        with self.assertRaisesRegex(ResultRejected, "unclassified warning"):
            self.plan.finish(self.result)

    def test_capacity_is_rejected_during_planning(self):
        self.execution["oversized_source_identity"] = "x" * MAX_RESULT_BYTES
        with self.assertRaisesRegex(ResultRejected, "before inference"):
            self.make_plan()

    def test_extreme_finite_shifts_fit_the_pre_execution_bound(self):
        for row in self.result["prediction"]["predictions"]:
            row["shift_ppm"] = -1.7976931348623157e308
        content = self.plan.finish(self.result)
        self.assertLessEqual(len(content), self.plan.maximum_result_bytes)

    def test_no_eligible_atoms_requires_no_loaded_heads(self):
        self.raw = b'{"coordinate_unit":"angstrom","atoms":["O"],"coordinates":[[0,0,0]]}'
        self.geometry = prepare_json(self.raw)
        plan = self.make_plan()
        self.result["request_sha256"] = hashlib.sha256(self.raw).hexdigest()
        self.result["prediction"]["predictions"] = []
        self.result["provenance"]["artifacts"]["elements"] = {}
        self.assertEqual(json.loads(plan.finish(self.result))["geometries"][0]["predictions"], [])
        self.result["provenance"]["artifacts"]["elements"]["H"] = self.artifacts["elements"]["H"]
        with self.assertRaises(ResultRejected):
            plan.finish(self.result)

    def test_128_atom_escaped_envelope_at_capacity_fits_with_warning(self):
        atoms = ["H", "C"] * 64
        self.raw = json.dumps({"coordinate_unit": "angstrom", "atoms": atoms, "coordinates": [[0, 0, 0]] * 128}).encode()
        self.geometry = prepare_json(self.raw)
        self.execution["fixture_metadata"] = ""
        base = self.make_plan().maximum_result_bytes
        text = "\n\\雪"
        encoded_width = len(json.dumps(text, ensure_ascii=True)) - 2
        self.execution["fixture_metadata"] = text * ((MAX_RESULT_BYTES - base) // encoded_width)
        plan = self.make_plan()
        self.assertLessEqual(plan.maximum_result_bytes, MAX_RESULT_BYTES)
        self.assertGreater(plan.maximum_result_bytes, MAX_RESULT_BYTES - encoded_width)
        extremes = (-1.7976931348623157e308, -2.2250738585072014e-308, -5e-324)
        self.result["request_sha256"] = hashlib.sha256(self.raw).hexdigest()
        self.result["prediction"]["predictions"] = [
            {"atom_index": i, "element": atom, "shift_ppm": extremes[i % len(extremes)]} for i, atom in enumerate(atoms)
        ]
        self.result["private_warnings"] = [
            {
                "category": "sklearn.exceptions.InconsistentVersionWarning",
                "message": "private",
                "filename": "/private",
                "lineno": 1,
            }
        ]
        self.assertLessEqual(len(plan.finish(self.result)), plan.maximum_result_bytes)
        self.execution["fixture_metadata"] += text
        with self.assertRaisesRegex(ResultRejected, "before inference"):
            self.make_plan()
