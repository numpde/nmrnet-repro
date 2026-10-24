"""H128/C128 through the supplied service path; synthetic capacity, not chemistry.

The fixed coordinate recipe is test_prediction_release.py::assert_synthetic_capacity
at 3bd6114. It supplies no expected shifts. SuppliedLifecycle independently checks
exact source/atom/coordinate correspondence and every atom-index/nucleus output.
"""

from pathlib import Path

from tests.integration.nmrnet.reference_service import ExportedServiceLifecycle
from tests.integration.nmrnet.reference_fixtures import require_capacity_inputs


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
