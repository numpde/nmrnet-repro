"""Counts alone cannot admit duplicate producers, substituted inputs or outputs."""

import copy
import hashlib
import json
import unittest

from tests.integration.nmrnet.reference_fixtures import (
    indexed_predictions,
    render_fixture,
    require_reference_inputs,
    verify_fixture,
)
from tests.integration.nmrnet.reference_ports import authored_populations
from tests.integration.nmrnet.reference_service import require_reference_predictions


class ReferenceFixtureRefusalTests(unittest.TestCase):
    def test_same_count_substitutions_and_missing_or_extra_rows_cannot_pass_the_numerical_gate(self):
        for change in ("shift", "atom", "nucleus", "producer", "missing", "extra", "historical_value", "historical_mask"):
            records, predictions, reference, production = authored_populations()
            if change == "shift":
                predictions[0][0]["shift_ppm"] += 0.0000001
            elif change == "atom":
                predictions[0][0]["atom_index"] = 127
            elif change == "nucleus":
                predictions[0][0]["element"] = "N"
            elif change == "producer":
                records[0]["source_key_hex"] = "00"
            elif change == "missing":
                predictions[0].pop()
            elif change == "extra":
                predictions[0].append({"element": "C", "atom_index": 127, "shift_ppm": 0.0})
            elif change == "historical_value":
                reference["elements"]["H"]["predictions"][0]["prediction_ppm"] += 0.0000001
            else:
                production["qualification"]["real_model_callable"]["exact_reference_population"][0]["atom_index"] = 127
            with self.subTest(change=change), self.assertRaises(AssertionError):
                require_reference_predictions(records, predictions, reference, production)

    def test_pinned_producers_and_original_request_refuse_self_consistent_local_substitution(self):
        original = b'{"coordinate_unit": "angstrom", "atoms": ["H"], "coordinates": [[0.1, 0.0, 0.0]]}'
        request = json.loads(original)
        source, manifest = render_fixture([("H", "01", request)], {("H", "01"): "a" * 64}, case="references")
        reference = {"elements": {"H": {"records": [{"key_hex": "01", "raw_sha256": "a" * 64}]}}}
        production = {
            "qualification": {
                "real_model_callable": {
                    "fixture_observations": [
                        {
                            "source_element": "H",
                            "source_key_hex": "01",
                            "source_record_sha256": "a" * 64,
                            "request": {"size": len(original), "sha256": hashlib.sha256(original).hexdigest()},
                        }
                    ]
                }
            }
        }
        for changes in ({"source_element": "C"}, {"source_key_hex": "02"}, {"raw_sha256": "b" * 64}):
            altered = copy.deepcopy(manifest)
            altered["records"][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(AssertionError):
                require_reference_inputs(altered, reference, production)
        request["coordinates"][0][0] = 0.2
        substituted_source, substituted = render_fixture([("H", "01", request)], {("H", "01"): "a" * 64}, case="references")
        verify_fixture(substituted_source, substituted)
        self.assertNotEqual(source, substituted_source)
        with self.assertRaisesRegex(AssertionError, "previously qualified request"):
            require_reference_inputs(substituted, reference, production)

    def test_duplicate_producer_or_prediction_identity_is_rejected(self):
        row = [{"element": "H", "atom_index": 0, "shift_ppm": 1.0}]
        for identities, predictions in (
            ([("H", "01"), ("H", "01")], [row, row]),
            ([("H", "01")], [row + row]),
        ):
            with self.subTest(identities=identities), self.assertRaisesRegex(AssertionError, "duplicate"):
                indexed_predictions(identities, predictions)

    def test_changed_source_hash_request_or_record_mapping_cannot_roundtrip(self):
        requests = [("H", "01", {"coordinate_unit": "angstrom", "atoms": ["H"], "coordinates": [[0.1, 0.0, 0.0]]})]
        source, original = render_fixture(requests, {("H", "01"): "a" * 64}, case="references")
        for change in ("source", "coordinates", "record", "positions"):
            with self.subTest(change=change):
                manifest = copy.deepcopy(original)
                content = source
                if change == "source":
                    content = source.replace(b"0.1", b"0.2")
                elif change == "coordinates":
                    manifest["records"][0]["original_request"]["coordinates"][0][0] = 0.2
                elif change == "record":
                    manifest["records"][0]["source_record_number"] = 2
                else:
                    manifest["records"][0]["source_positions"] = [4]
                with self.assertRaises(AssertionError):
                    verify_fixture(content, manifest)
