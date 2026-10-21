"""Corrupted science and extra interpretation exchanges fail qualification."""

from tools.container_guard import require_container

require_container()

import math
import struct
import unittest
from unittest.mock import patch

from api_prediction import compare_science, compare_scientific_documents
from prediction_test_ports import scientific_documents


class ApiPredictionAdversarialTests(unittest.TestCase):
    def test_real_atom_order_binary32_coordinate_prediction_and_interpretation_changes_fail(self):
        for fault in ("order", "binary32", "prediction", "unit", "head", "model", "turns", "endpoint"):
            with self.subTest(fault=fault):
                actual, retained, receipt, metadata, interpreter, _source = scientific_documents()
                geometry = actual["geometries"][0]
                if fault == "order":
                    geometry["prepared_request"]["atoms"][:2] = ["Cl", "H"]
                elif fault == "binary32":
                    geometry["float32_coordinates"][0][0] += 0.25
                elif fault == "prediction":
                    geometry["predictions"][0]["shift_ppm"] += 0.0001
                elif fault == "unit":
                    geometry["preparation"]["source_unit"] = "angstrom"
                elif fault == "head":
                    geometry["loaded_elements"] = ["H"]
                elif fault == "model":
                    actual["selected_models"]["H"] = "changed-checkpoint"
                elif fault == "turns":
                    actual["interpretation"]["turns_started"] = 2
                else:
                    actual["interpretation"]["routes"][0]["endpoint"]["model"] = "different-model"
                with self.assertRaises(AssertionError):
                    compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter)

    def test_changed_binary64_value_cannot_pass_merely_because_binary32_is_unchanged(self):
        actual, retained, receipt, metadata, interpreter, _source = scientific_documents()
        request = actual["geometries"][0]["prepared_request"]
        original = request["coordinates"][0][0]
        changed = math.nextafter(original, math.inf)
        self.assertEqual(struct.pack("f", original), struct.pack("f", changed))
        request["coordinates"][0][0] = changed
        with self.assertRaisesRegex(AssertionError, "prepared_request"):
            compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter)

    def test_pinned_retained_receipt_hash_is_still_checked_before_comparison(self):
        with (
            patch("api_prediction.Path.read_bytes", return_value=b"{}"),
            self.assertRaisesRegex(AssertionError, "independent retained"),
        ):
            compare_science(b"{}", {}, {}, interpreter=None)

    def test_scripted_source_positions_remain_exact(self):
        actual, retained, receipt, metadata, _interpreter, _source = scientific_documents()
        actual["geometries"][0]["source"]["positions"][0] = 2
        with self.assertRaises(AssertionError):
            compare_scientific_documents(actual, retained, receipt, metadata, interpreter=None)

    def test_real_source_identity_and_actual_line_positions_are_required(self):
        for fault in (
            "missing-sha256",
            "missing-byte_length",
            "missing-positions",
            "bad-digest",
            "empty-length",
            "wrong-count",
            "repeated-line",
        ):
            with self.subTest(fault=fault):
                actual, retained, receipt, metadata, interpreter, _text = scientific_documents()
                source = actual["geometries"][0]["source"]
                if fault.startswith("missing-"):
                    del source[fault.removeprefix("missing-")]
                elif fault == "bad-digest":
                    source["sha256"] = "unverified"
                elif fault == "empty-length":
                    source["byte_length"] = 0
                elif fault == "wrong-count":
                    source["positions"].pop()
                else:
                    source["positions"][1] = source["positions"][0]
                with self.assertRaises(AssertionError):
                    compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter)
