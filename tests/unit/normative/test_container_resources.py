"""Resource observations are bounded kernel facts, not inferred scientific success."""

from tools.container_guard import require_container

require_container()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import container_resources


class ContainerResourcesNormativeTests(unittest.TestCase):
    def test_reads_the_fixed_cgroup_files_without_inventing_missing_facts(self):
        facts = {
            "cpu.max": "200000 100000",
            "cpu.stat": "usage_usec 71\nuser_usec 50\nsystem_usec 21",
            "memory.max": "536870912",
            "memory.peak": "4132864",
            "memory.swap.max": "0",
            "pids.max": "64",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, value in facts.items():
                (root / name).write_text(value + "\n")
            with patch.object(container_resources, "CGROUP_ROOT", root):
                self.assertEqual(container_resources.cgroup_snapshot(), facts)
