"""Capacity evidence validates measured cgroup facts without claiming accuracy."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.integration.nmrnet.capacity_service import require_capacity_inputs
from tests.integration.nmrnet.resource_evidence import require_cgroup_resources, retired_worker_resources


def resource_facts():
    return {
        "cpu.max": "200000 100000",
        "cpu.stat": "usage_usec 71\nuser_usec 50\nsystem_usec 21\nnr_throttled 0",
        "memory.max": "536870912",
        "memory.peak": "4132864",
        "memory.swap.max": "0",
        "pids.max": "64",
    }


class CapacityResourceTests(unittest.TestCase):
    def test_fixed_128_atom_input_recipe_is_capacity_only(self):
        manifest = capacity_manifest()
        require_capacity_inputs(manifest)
        self.assertEqual([len(item["original_request"]["atoms"]) for item in manifest["records"]], [128, 128])

    def test_fixed_limits_and_positive_kernel_observations_are_preserved(self):
        facts = resource_facts()
        self.assertEqual(require_cgroup_resources(facts, memory_bytes=536870912), facts)

    def test_completed_report_joins_the_exact_retired_role_and_allocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            allocation = "a" * 32
            report = {
                "allocation": allocation,
                "role": "reader",
                "scope": "cgroup_through_final_snapshot",
                "cgroup": resource_facts(),
            }
            (root / "result").write_text(f"retired {allocation}\n")
            (root / "resources.json").write_text(json.dumps(report))
            self.assertEqual(retired_worker_resources(root, role="reader", allocation=allocation), report)


def capacity_manifest():
    return {
        "records": [
            {
                "source_element": element,
                "source_key_hex": "313238",
                "original_request": {
                    "coordinate_unit": "angstrom",
                    "atoms": [element] * 128,
                    "coordinates": [[index * 0.1, 0.0, 0.0] for index in range(128)],
                },
            }
            for element in ("H", "C")
        ]
    }
