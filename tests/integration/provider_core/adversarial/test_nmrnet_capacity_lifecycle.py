"""Absent, foreign or fabricated resource evidence cannot qualify real capacity."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.integration.nmrnet.capacity_service import require_capacity_inputs
from tests.integration.nmrnet.normative.test_nmrnet_capacity_lifecycle import capacity_manifest, resource_facts
from tests.integration.nmrnet.resource_evidence import require_cgroup_resources, retired_worker_resources


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

    def test_missing_oversized_wrong_allocation_role_or_scope_report_fails_after_retirement(self):
        for change in ("missing", "oversized", "allocation", "role", "scope", "malformed"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                token = "a" * 32
                (root / "result").write_text(f"retired {token}\n")
                report = {
                    "allocation": token,
                    "role": "reader",
                    "scope": "cgroup_through_final_snapshot",
                    "cgroup": resource_facts(),
                }
                if change in ("allocation", "role", "scope"):
                    report[change] = "foreign"
                if change == "oversized":
                    content = b"x" * 16385
                elif change == "malformed":
                    content = b"not JSON"
                else:
                    content = json.dumps(report).encode()
                if change != "missing":
                    (root / "resources.json").write_bytes(content)
                with self.assertRaises((AssertionError, FileNotFoundError, ValueError)):
                    retired_worker_resources(root, role="reader", allocation=token)
                self.assertEqual((root / "result").read_text(), f"retired {token}\n")
