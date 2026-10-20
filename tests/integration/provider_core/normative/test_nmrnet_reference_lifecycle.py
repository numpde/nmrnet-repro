"""Input export and keyed numerical populations preserve scientific correspondence."""

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


class ReferenceFixtureTests(unittest.TestCase):
    def test_full_population_and_independent_historical_mask_are_compared_exactly(self):
        observed = require_reference_predictions(*authored_populations())
        self.assertEqual(observed, {"complete_predictions": 137, "by_nucleus": {"H": 82, "C": 55}, "historical_positions": 64})

    def test_original_request_identity_and_executed_encoding_are_distinct_but_values_exact(self):
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
        require_reference_inputs(manifest, reference, production)
        (prepared,) = verify_fixture(source, manifest)
        self.assertNotEqual(prepared.request_bytes, original)
        self.assertNotEqual(hashlib.sha256(prepared.request_bytes).hexdigest(), hashlib.sha256(original).hexdigest())
        self.assertEqual(json.loads(prepared.request_bytes), request)

    def test_xyz_roundtrip_preserves_values_order_source_records_and_line_positions(self):
        requests = [
            (
                "H",
                "01",
                {
                    "coordinate_unit": "angstrom",
                    "atoms": ["Cl", "H"],
                    "coordinates": [[-0.0, 0.10000000000000002, 1e-20], [1.78, 0.0, -0.3]],
                },
            ),
            ("C", "02", {"coordinate_unit": "angstrom", "atoms": ["C"], "coordinates": [[1.25, 0.0, 0.0]]}),
        ]
        origins = {("H", "01"): "a" * 64, ("C", "02"): "b" * 64}
        source, manifest = render_fixture(requests, origins, case="references")
        prepared = verify_fixture(source, manifest)
        self.assertEqual([item.source_record_number for item in prepared], [1, 2])
        self.assertEqual([item.source_positions for item in prepared], [(3, 4), (7,)])
        for item, (_, _, expected) in zip(prepared, requests, strict=True):
            self.assertEqual(json.loads(item.request_bytes), expected)
        self.assertIn(b"-0.0", source)

    def test_population_keys_include_producer_nucleus_and_atom_index(self):
        identities = [("H", "01"), ("C", "01")]
        rows = [
            [{"element": "C", "atom_index": 0, "shift_ppm": 10.25}],
            [{"element": "H", "atom_index": 0, "shift_ppm": 1.5}],
        ]
        self.assertEqual(
            indexed_predictions(identities, rows),
            {
                ("H", "01", "C", 0): 10.25,
                ("C", "01", "H", 0): 1.5,
            },
        )
