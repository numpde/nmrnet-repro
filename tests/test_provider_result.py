"""Scientific result admission preserves inputs and rejects substituted outcomes."""

from tools.container_guard import require_container

require_container()

import copy
import hashlib
import json
import unittest
from pathlib import Path

from nmrnet_provider.input_geometry import UploadSource, prepare_json
from nmrnet_provider.prepared_input import OutputSettings, PreparedJobInput
from nmrnet_provider.result import MAX_RESULT_BYTES, ResultPlan, ResultRejected
from nmrnet_provider.xyz import prepare_xyz


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
            PreparedJobInput((self.geometry,), "direct_coordinate_json", output=OutputSettings(shift_significant_digits=17)),
            job_ref="job:fixture",
            execution_attempt_ref="execution_attempt:sha256:" + "e" * 64,
            input_fingerprint="sha256:" + hashlib.sha256(self.raw).hexdigest(),
            execution_identity=self.execution,
            selected_artifacts=self.artifacts,
            runtime_expectations=self.runtime,
        )

    def batch_plan(self, geometries, method, source_upload=None):
        return ResultPlan(
            PreparedJobInput(geometries, method, source_upload, output=OutputSettings(shift_significant_digits=17)),
            job_ref="job:fixture",
            execution_attempt_ref="execution_attempt:sha256:" + "e" * 64,
            input_fingerprint="sha256:" + hashlib.sha256(self.raw).hexdigest(),
            execution_identity=self.execution,
            selected_artifacts=self.artifacts,
            runtime_expectations=self.runtime,
        )

    def test_uploaded_source_identity_is_frozen_with_mapping_and_public_result(self):
        source = UploadSource("job:fixture", "upload:sha256:" + "a" * 64, len(self.raw), self.geometry.source_sha256)
        plan = self.batch_plan((self.geometry,), "xyz", source)
        identity = json.loads(plan.prepared_identity)
        public = json.loads(plan.finish((self.result,)))
        for envelope in (identity, public):
            retained = envelope["geometries"][0]["source"]
            self.assertEqual(retained["upload_ref"], source.upload_ref)
            self.assertEqual(retained["byte_length"], len(self.raw))
            self.assertEqual(retained["sha256"], self.geometry.source_sha256)

    def test_other_job_or_source_digest_cannot_supply_upload_identity(self):
        for job_ref, digest in (("job:other", self.geometry.source_sha256), ("job:fixture", "f" * 64)):
            with self.subTest(job_ref=job_ref, digest=digest), self.assertRaisesRegex(ValueError, "verified source"):
                self.batch_plan((self.geometry,), "xyz", UploadSource(job_ref, "upload:fixture", len(self.raw), digest))

    def outcome(self, geometry, value):
        atoms = json.loads(geometry.request_bytes)["atoms"]
        heads = sorted(set(atoms) & {"H", "C"})
        return {
            "request_sha256": hashlib.sha256(geometry.request_bytes).hexdigest(),
            "prediction": {
                "unit": "ppm",
                "predictions": [
                    {"atom_index": index, "element": atom, "shift_ppm": value}
                    for index, atom in enumerate(atoms)
                    if atom in heads
                ],
            },
            "provenance": {
                "artifacts": {
                    "dictionary": self.artifacts["dictionary"],
                    "elements": {head: self.artifacts["elements"][head] for head in heads},
                },
                "runtime": self.runtime,
            },
            "private_warnings": [],
        }

    def test_batch_keeps_different_and_repeated_selected_geometries_in_order(self):
        source = (Path(__file__).parent / "fixtures/preparation/normative/mixed_records.xyz").read_bytes()
        geometries = prepare_xyz(source, source_unit="angstrom", selection=(2, 1, 3))
        plan = self.batch_plan(geometries, "xyz")
        outcomes = tuple(self.outcome(geometry, value) for geometry, value in zip(geometries, (1.2, 42.5, 43.5), strict=True))
        document = json.loads(plan.finish(outcomes))
        identity = json.loads(plan.prepared_identity)
        self.assertEqual([2, 1, 3], [item["source"]["record_number"] for item in document["geometries"]])
        self.assertEqual(["geometry-1", "geometry-2", "geometry-3"], [item["geometry_id"] for item in document["geometries"]])
        self.assertEqual(
            [[10, 11, 12], [3, 4, 5, 6, 7], [15, 16, 17, 18, 19]],
            [item["source"]["positions"] for item in identity["geometries"]],
        )
        for item, outcome in zip(document["geometries"], outcomes, strict=True):
            self.assertEqual(outcome["prediction"]["predictions"], item["predictions"])
            self.assertEqual(outcome["request_sha256"], item["request_sha256"])
        self.assertLessEqual(len(plan.finish(outcomes)), plan.maximum_result_bytes)

    def test_batch_requires_all_outcomes_in_order_and_rejects_later_substitution(self):
        other = prepare_json(b'{"coordinate_unit":"angstrom","atoms":["C"],"coordinates":[[0,0,0]]}')
        geometries = (self.geometry, other)
        plan = self.batch_plan(geometries, "direct_coordinate_json")
        outcomes = tuple(self.outcome(geometry, 1.2) for geometry in geometries)
        bad = copy.deepcopy(outcomes[1])
        bad["prediction"]["predictions"][0]["element"] = "H"
        drift = copy.deepcopy(outcomes[1])
        drift["provenance"]["runtime"]["python"] = "PRIVATE_RUNTIME_DRIFT"
        for changed in (outcomes[:1], outcomes + outcomes[:1], outcomes[::-1], (outcomes[0], bad), (outcomes[0], drift)):
            with self.subTest(count=len(changed)), self.assertRaises(ResultRejected):
                plan.finish(changed)
        self.assertEqual(2, len(json.loads(plan.finish(outcomes))["geometries"]))

    def test_batch_observations_cannot_leak_loaded_heads_or_warnings_between_geometries(self):
        other = prepare_json(b'{"coordinate_unit":"angstrom","atoms":["C"],"coordinates":[[0,0,0]]}')
        geometries = (self.geometry, other)
        plan = self.batch_plan(geometries, "direct_coordinate_json")
        outcomes = tuple(self.outcome(geometry, 1.2) for geometry in geometries)
        warning = {
            "category": "builtins.UserWarning",
            "message": "PRIVATE_BATCH_WARNING",
            "filename": "/private",
            "lineno": 1,
            "head": "H",
            "versions": None,
        }
        outcomes[0]["private_warnings"] = [warning]
        content = plan.finish(outcomes)
        self.assertNotIn(b"PRIVATE_BATCH_WARNING", content)
        self.assertEqual("geometry-1", json.loads(content)["warnings"][0]["geometry_id"])
        outcomes[1]["private_warnings"] = [warning]
        with self.assertRaisesRegex(ResultRejected, "geometry-2.*not loaded"):
            plan.finish(outcomes)
        outcomes[1]["private_warnings"] = []
        outcomes[1]["provenance"]["artifacts"]["elements"]["H"] = self.artifacts["elements"]["H"]
        with self.assertRaisesRegex(ResultRejected, "geometry-2.*loaded artifacts"):
            plan.finish(outcomes)

    def test_complete_batch_capacity_is_rejected_even_when_each_geometry_fits(self):
        raw = json.dumps({"coordinate_unit": "angstrom", "atoms": ["H", "C"] * 64, "coordinates": [[0, 0, 0]] * 128}).encode()
        geometry = prepare_json(raw)
        self.assertLess(self.batch_plan((geometry,), "direct_coordinate_json").maximum_result_bytes, MAX_RESULT_BYTES)
        with self.assertRaisesRegex(ResultRejected, "before inference"):
            self.batch_plan((geometry,) * 128, "direct_coordinate_json")

    def test_batch_requires_a_nonempty_immutable_geometry_collection(self):
        for geometries in ((), [self.geometry], (object(),)):
            with self.subTest(value=type(geometries).__name__), self.assertRaises(TypeError):
                self.batch_plan(geometries, "xyz")

    def test_missing_required_model_selection_fails_during_planning(self):
        for absent in ("dictionary", "H", "C"):
            with self.subTest(absent=absent):
                artifacts = copy.deepcopy(self.artifacts)
                if absent == "dictionary":
                    del self.artifacts["dictionary"]
                else:
                    del self.artifacts["elements"][absent]
                try:
                    with self.assertRaisesRegex(ValueError, "required model selection"):
                        self.make_plan()
                finally:
                    self.artifacts = artifacts

    def test_batch_at_complete_byte_limit_includes_each_geometry_warning_and_extreme_shift(self):
        geometries = (self.geometry, self.geometry)
        self.execution["fixture_metadata"] = ""
        base = self.batch_plan(geometries, "direct_coordinate_json").maximum_result_bytes
        text = "\n\\雪"
        width = len(json.dumps(text, ensure_ascii=True)) - 2
        self.execution["fixture_metadata"] = text * ((MAX_RESULT_BYTES - base) // width)
        plan = self.batch_plan(geometries, "direct_coordinate_json")
        outcomes = tuple(self.outcome(geometry, -1.7976931348623157e308) for geometry in geometries)
        for outcome in outcomes:
            outcome["private_warnings"] = [
                {
                    "category": "sklearn.exceptions.InconsistentVersionWarning",
                    "message": "PRIVATE_BATCH_WARNING",
                    "filename": "/private",
                    "lineno": 1,
                    "head": head,
                    "versions": {"original": "1." + "9" * 30, "current": "2." + "9" * 30},
                }
                for head in ("H", "C")
            ] + [
                {
                    "category": "builtins.UserWarning",
                    "message": "PRIVATE_BATCH_WARNING",
                    "filename": "/private",
                    "lineno": 1,
                    "head": head,
                    "versions": None,
                }
                for head in ("H", "C")
            ]
        content = plan.finish(outcomes)
        self.assertLessEqual(len(content), plan.maximum_result_bytes)
        self.assertGreater(plan.maximum_result_bytes, MAX_RESULT_BYTES - width)
        self.assertEqual({"geometry-1", "geometry-2"}, {warning["geometry_id"] for warning in json.loads(content)["warnings"]})
        self.assertNotIn(b"PRIVATE_BATCH_WARNING", content)
        self.execution["fixture_metadata"] += text
        with self.assertRaisesRegex(ResultRejected, "before inference"):
            self.batch_plan(geometries, "direct_coordinate_json")

    def test_prepared_identity_retains_ordered_source_and_request_without_coordinates(self):
        document = json.loads(self.plan.prepared_identity)
        self.assertEqual(self.execution, document["execution"])
        self.assertEqual(self.artifacts, document["selected_models"])
        self.assertEqual(self.runtime, document["runtime"])
        (geometry,) = document["geometries"]
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), geometry["request_sha256"])
        self.assertEqual(self.geometry.source_sha256, geometry["source"]["sha256"])
        self.assertEqual(list(self.geometry.source_positions), geometry["source"]["positions"])
        self.assertEqual("angstrom", geometry["preparation"]["source_unit"])
        self.assertEqual({"geometry_id", "request_sha256", "source", "preparation", "required_elements"}, set(geometry))
        self.assertEqual(["C", "H"], geometry["required_elements"])
        self.execution["source_revision"] = "changed after admission"
        document["geometries"].clear()
        self.assertEqual(1, len(json.loads(self.plan.prepared_identity)["geometries"]))
        self.assertEqual("d" * 40, json.loads(self.plan.prepared_identity)["execution"]["source_revision"])

    def test_result_preserves_prepared_and_float32_coordinates_and_source_mapping(self):
        result = json.loads(self.plan.finish((self.result,)))
        self.assertEqual(result["schema_id"], "nmrnet.coordinate_shifts.result.v2")
        geometry = result["geometries"][0]
        self.assertEqual(geometry["prepared_request"], json.loads(self.raw))
        self.assertNotEqual(geometry["float32_coordinates"][1][0], 1.09)
        self.assertEqual(geometry["source"]["positions"], [0, 1, 2])
        self.assertEqual(geometry["predictions"], self.result["prediction"]["predictions"])
        self.assertEqual(geometry["request_sha256"], hashlib.sha256(self.raw).hexdigest())

    def test_hydrogen_coverage_describes_supplied_atoms_without_guessing_completeness(self):
        """Synthetic coordinates test recipient bookkeeping, not molecular accuracy."""
        # The fixture author intends complete/partial chloromethane and H-free
        # carbon tetrachloride. Coordinates alone cannot establish those identities.
        for name, atoms in (
            ("supplied_H", ["H", "Cl", "C", "H", "H"]),
            ("partial_H", ["H", "Cl", "C"]),
            ("omitted_H", ["Cl", "C"]),
            ("H_free", ["Cl", "C", "Cl", "Cl", "Cl"]),
        ):
            with self.subTest(case=name):
                self.raw = json.dumps(
                    {"coordinate_unit": "angstrom", "atoms": atoms, "coordinates": [[index, 0, 0] for index in range(len(atoms))]}
                ).encode()
                self.geometry = prepare_json(self.raw)
                plan = self.make_plan()
                self.result["request_sha256"] = hashlib.sha256(self.raw).hexdigest()
                self.result["prediction"]["predictions"] = [
                    {"atom_index": index, "element": atom, "shift_ppm": 1.0}
                    for index, atom in enumerate(atoms)
                    if atom in {"H", "C"}
                ]
                self.result["provenance"]["artifacts"]["elements"] = {
                    head: self.artifacts["elements"][head] for head in sorted(set(atoms) & {"H", "C"})
                }
                document = json.loads(plan.finish((self.result,)))
                geometry = document["geometries"][0]
                self.assertEqual(atoms, geometry["prepared_request"]["atoms"])
                self.assertEqual(atoms.count("H"), geometry["coverage"]["supplied_hydrogen_atoms"])
                self.assertEqual(atoms.count("H"), geometry["coverage"]["predicted_hydrogen_atoms"])
                self.assertEqual("not_established_from_coordinates", geometry["coverage"]["hydrogen_completeness"])
                self.assertEqual(self.result["prediction"]["predictions"], geometry["predictions"])
                limits = " ".join(document["scientific_limits"])
                self.assertIn("not added", limits)
                self.assertIn("genuinely absent", limits)
                self.assertIn("separately authorized", limits)

    def test_smiles_label_cannot_turn_supplied_coordinates_into_generated_evidence(self):
        with self.assertRaisesRegex(ValueError, "implemented supplied-coordinate"):
            ResultPlan(
                PreparedJobInput((self.geometry,), "smiles"),
                job_ref="job:fixture",
                execution_attempt_ref="execution_attempt:sha256:" + "e" * 64,
                input_fingerprint="sha256:" + hashlib.sha256(self.raw).hexdigest(),
                execution_identity=self.execution,
                selected_artifacts=self.artifacts,
                runtime_expectations=self.runtime,
            )

    def test_plan_is_not_changed_by_later_caller_mutation(self):
        self.execution["image_id"] = "substituted"
        self.artifacts["dictionary"]["sha256"] = "substituted"
        self.runtime["python"] = "substituted"
        result = json.loads(self.plan.finish((self.result,)))
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
                    self.plan.finish((result,))
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
                    self.plan.finish((result,))

    def test_raw_warning_text_and_paths_never_enter_public_result(self):
        self.result["private_warnings"] = [
            {
                "category": "sklearn.exceptions.InconsistentVersionWarning",
                "message": "private-canary",
                "filename": "/private-canary",
                "lineno": 17,
                "head": "H",
                "versions": {"original": "1.5.2", "current": "1.8.0"},
            }
        ]
        content = self.plan.finish((self.result,))
        self.assertNotIn(b"private-canary", content)
        self.assertEqual(json.loads(content)["warnings"][0]["code"], "scaler_version_mismatch")
        self.assertEqual(json.loads(content)["warnings"][0]["head"], "H")
        self.assertEqual(json.loads(content)["warnings"][0]["geometry_id"], "geometry-1")
        self.assertEqual(json.loads(content)["warnings"][0]["versions"], {"original": "1.5.2", "current": "1.8.0"})
        self.result["private_warnings"][0]["category"] = "unknown.private-canary"
        self.result["private_warnings"][0]["versions"] = None
        content = self.plan.finish((self.result,))
        self.assertNotIn(b"private-canary", content)
        result = json.loads(content)
        self.assertEqual(result["warnings"][0]["code"], "additional_scaler_warnings")
        self.assertEqual(result["warnings"][0]["heads"], ["H"])
        self.assertEqual(result["geometries"][0]["predictions"], self.result["prediction"]["predictions"])

    def test_warning_projection_is_bounded_without_discarding_private_observations(self):
        warning = {
            "category": "sklearn.exceptions.InconsistentVersionWarning",
            "message": "private",
            "filename": "/private",
            "lineno": 1,
            "head": "H",
            "versions": {"original": "1.5.2", "current": "1.8.0"},
        }
        self.result["private_warnings"] = [copy.deepcopy(warning) for _ in range(20)]
        self.assertEqual(len(json.loads(self.plan.finish((self.result,)))["warnings"]), 1)
        self.assertEqual(len(self.result["private_warnings"]), 20)
        self.result["private_warnings"][0]["versions"]["original"] = "private-canary"
        content = self.plan.finish((self.result,))
        self.assertNotIn(b"private-canary", content)
        self.assertIsNone(json.loads(content)["warnings"][0]["versions"])
        self.result["private_warnings"][0]["lineno"] = "malformed"
        with self.assertRaises(ResultRejected):
            self.plan.finish((self.result,))

    def test_capacity_is_rejected_during_planning(self):
        self.execution["oversized_source_identity"] = "x" * MAX_RESULT_BYTES
        with self.assertRaisesRegex(ResultRejected, "before inference"):
            self.make_plan()

    def test_caller_cannot_widen_the_reserved_result_size_after_planning(self):
        reserved = self.plan.maximum_result_bytes
        with self.assertRaises(AttributeError):
            self.plan.maximum_result_bytes = MAX_RESULT_BYTES
        self.assertEqual(reserved, self.plan.maximum_result_bytes)
        self.assertLessEqual(len(self.plan.finish((self.result,))), reserved)

    def test_warning_cannot_claim_a_scaler_that_was_not_loaded(self):
        for atom in ("H", "O"):
            with self.subTest(atom=atom):
                self.raw = json.dumps({"coordinate_unit": "angstrom", "atoms": [atom], "coordinates": [[0, 0, 0]]}).encode()
                self.geometry = prepare_json(self.raw)
                self.result["request_sha256"] = hashlib.sha256(self.raw).hexdigest()
                self.result["prediction"]["predictions"] = (
                    [{"atom_index": 0, "element": "H", "shift_ppm": 1.2}] if atom == "H" else []
                )
                self.result["provenance"]["artifacts"]["elements"] = {"H": self.artifacts["elements"]["H"]} if atom == "H" else {}
                self.result["private_warnings"] = [
                    {
                        "category": "builtins.UserWarning",
                        "message": "private",
                        "filename": "/private",
                        "lineno": 1,
                        "head": "C",
                        "versions": None,
                    }
                ]
                with self.assertRaisesRegex(ResultRejected, "not loaded"):
                    self.make_plan().finish((self.result,))

    def test_extreme_finite_shifts_fit_the_pre_execution_bound(self):
        for row in self.result["prediction"]["predictions"]:
            row["shift_ppm"] = -1.7976931348623157e308
        content = self.plan.finish((self.result,))
        self.assertLessEqual(len(content), self.plan.maximum_result_bytes)

    def test_no_eligible_atoms_requires_no_loaded_heads(self):
        self.raw = b'{"coordinate_unit":"angstrom","atoms":["O"],"coordinates":[[0,0,0]]}'
        self.geometry = prepare_json(self.raw)
        plan = self.make_plan()
        self.result["request_sha256"] = hashlib.sha256(self.raw).hexdigest()
        self.result["prediction"]["predictions"] = []
        self.result["provenance"]["artifacts"]["elements"] = {}
        self.assertEqual(json.loads(plan.finish((self.result,)))["geometries"][0]["predictions"], [])
        self.result["provenance"]["artifacts"]["elements"]["H"] = self.artifacts["elements"]["H"]
        with self.assertRaises(ResultRejected):
            plan.finish((self.result,))

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
                "head": head,
                "versions": {"original": "1." + "9" * 30, "current": "2." + "9" * 30},
            }
            for head in ("H", "C")
        ]
        self.result["private_warnings"].extend(
            {
                "category": "builtins.UserWarning",
                "message": "private",
                "filename": "/private",
                "lineno": 2,
                "head": head,
                "versions": None,
            }
            for head in ("H", "C")
        )
        self.assertLessEqual(len(plan.finish((self.result,))), plan.maximum_result_bytes)
        self.execution["fixture_metadata"] += text
        with self.assertRaisesRegex(ResultRejected, "before inference"):
            self.make_plan()
