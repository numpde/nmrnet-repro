"""Missing and oversized kernel observations fail without disclosing their contents."""

from tools.container_guard import require_container

require_container()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import container_resources


class ContainerResourcesAdversarialTests(unittest.TestCase):
    def test_missing_kernel_file_is_not_replaced_with_a_default(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(container_resources, "CGROUP_ROOT", Path(directory)),
            self.assertRaises(FileNotFoundError),
        ):
            container_resources.cgroup_snapshot()

    def test_oversized_kernel_file_does_not_enter_a_report_or_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cpu.max").write_bytes(b"private-canary" * 400)
            with patch.object(container_resources, "CGROUP_ROOT", root), self.assertRaises(ValueError) as raised:
                container_resources.cgroup_snapshot()
            self.assertIn("cpu.max", str(raised.exception))
            self.assertNotIn("private-canary", str(raised.exception))
