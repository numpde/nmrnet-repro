"""Compose qualification must admit its CLI before reading runtime fixtures."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path.cwd()
BOOTSTRAP = ROOT / "tests/integration/provider_runtime/compose_peer.py"


class ComposePeerBootstrapTests(unittest.TestCase):
    def run_cli(self, arguments):
        program = """
import runpy
import sys
from unittest.mock import patch

invoke = runpy.run_path
script = sys.argv[1]
sys.argv = sys.argv[1:]
with patch("shutil.copytree", side_effect=AssertionError("CLI admission copied fixtures")), patch(
    "runpy.run_path", side_effect=AssertionError("CLI admission loaded the runtime")
):
    invoke(script, run_name="__main__")
"""
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-B", "-c", program, str(BOOTSTRAP), *arguments],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual([], list(Path(directory).iterdir()))
        return result

    def test_help_needs_no_fixture_or_runtime_dependency(self):
        result = self.run_cli(["--help"])
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("--delivery-project", result.stdout)
        self.assertEqual("", result.stderr)

    def test_missing_arguments_reject_before_fixture_or_runtime_effects(self):
        result = self.run_cli(["--case", "pasted"])
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        self.assertIn("required", result.stderr)
        self.assertIn("--root", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
