"""Absent, foreign or fabricated resource evidence cannot qualify real capacity."""

import unittest

from tests.integration.nmrnet.reference_fixtures import require_capacity_inputs
from tests.integration.nmrnet.normative.test_nmrnet_capacity_lifecycle import capacity_manifest, resource_facts
from tests.integration.nmrnet.resource_evidence import require_cgroup_resources


class CapacityResourceRefusalTests(unittest.TestCase):
    def test_smaller_changed_or_reordered_capacity_input_cannot_pass(self):
        for change in ("smaller", "coordinate", "producer", "order"):
            manifest = capacity_manifest()
            if change == "smaller":
                manifest["records"][0]["original_request"]["atoms"].pop()
                manifest["records"][0]["original_request"]["coordinates"].pop()
            elif change == "coordinate":
                manifest["records"][0]["original_request"]["coordinates"][127][0] = 12.0
            elif change == "producer":
                manifest["records"][0]["source_key_hex"] = "00"
            else:
                manifest["records"].reverse()
            with self.subTest(change=change), self.assertRaises(AssertionError):
                require_capacity_inputs(manifest)

    def test_wrong_limits_nonpositive_peak_or_malformed_cpu_usage_are_not_qualified(self):
        for change in (
            {"cpu.max": "max 100000"},
            {"memory.max": "4294967296"},
            {"memory.peak": "0"},
            {"memory.peak": "536870913"},
            {"memory.swap.max": "max"},
            {"pids.max": "max"},
            {"cpu.stat": "usage_usec 0\nuser_usec 0\nsystem_usec 0"},
            {"cpu.stat": "usage_usec 71\nusage_usec 72\nuser_usec 50\nsystem_usec 21"},
            {"cpu.stat": "usage_usec private-canary"},
        ):
            facts = {**resource_facts(), **change}
            with self.subTest(field=next(iter(change))), self.assertRaises(AssertionError) as raised:
                require_cgroup_resources(facts, memory_bytes=536870912)
            self.assertNotIn("private-canary", str(raised.exception))
