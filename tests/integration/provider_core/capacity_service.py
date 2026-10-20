"""H128/C128 through the supplied service path; synthetic capacity, not chemistry.

The fixed coordinate recipe is test_prediction_release.py::assert_synthetic_capacity
at 3bd6114. It supplies no expected shifts. SuppliedLifecycle independently checks
exact source/atom/coordinate correspondence and every atom-index/nucleus output.
"""

from pathlib import Path

from tests.integration.nmrnet.reference_service import ExportedServiceLifecycle


def require_capacity_inputs(manifest):
    records = manifest["records"]
    if [(item["source_element"], item["source_key_hex"]) for item in records] != [("H", "313238"), ("C", "313238")]:
        raise AssertionError("Capacity input must contain exactly the ordered H128 and C128 producers")
    for element, record in zip(("H", "C"), records, strict=True):
        expected = {
            "coordinate_unit": "angstrom",
            "atoms": [element] * 128,
            "coordinates": [[index * 0.1, 0.0, 0.0] for index in range(128)],
        }
        if record["original_request"] != expected:
            raise AssertionError("Capacity input differs from the fixed independent 128-atom coordinate recipe")


class CapacityServiceLifecycle(ExportedServiceLifecycle):
    scientific_case = "capacity"

    def load_case(self):
        super().load_case()
        require_capacity_inputs(self.manifest)

    def fixture_sources(self):
        return [
            *super().fixture_sources(),
            ("tests/integration/provider_core/capacity_service.py", Path(__file__)),
        ]

    def qualification_details(self, content):
        return {
            **super().qualification_details(content),
            "scientific_fixture": "synthetic_H128_C128_capacity_only",
            "atoms_per_geometry": 128,
            "predictions_by_nucleus": {"H": 128, "C": 128},
            "tensor_scope": "Existing 002 proof observes 128 atoms / 130 tokens / 136 padded positions; this service proof observes input/output capacity.",
        }
