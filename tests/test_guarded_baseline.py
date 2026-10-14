"""Exercise accidental-host rejection in fresh, already contained processes."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


MODULES = ("baseline_prepare", "baseline_run", "baseline_model", "baseline_output", "baseline_result")


class GuardedBaselineTests(unittest.TestCase):
    def test_missing_or_invalid_marker_blocks_import_and_both_entrypoints(self):
        with tempfile.TemporaryDirectory() as directory:
            for module in MODULES:
                invocations = (["-c", f"import tools.{module}"],
                               ["-m", f"tools.{module}", "--help"],
                               [str(Path("tools", module + ".py").resolve()), "--help"])
                for marker in (None, "0"):
                    environment = {**os.environ, "PYTHONPATH": str(Path.cwd())}
                    environment.pop("NMRNET_IN_CONTAINER", None)
                    if marker is not None:
                        environment["NMRNET_IN_CONTAINER"] = marker
                    for invocation in invocations:
                        with self.subTest(module=module, marker=marker, invocation=invocation):
                            result = subprocess.run([sys.executable, "-B", *invocation], cwd=directory,
                                env=environment, text=True, capture_output=True)
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn("maintained Make target", result.stderr)
                            self.assertNotIn("Traceback", result.stderr)
                            self.assertEqual(result.stdout, "")
                            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_exact_marker_permits_real_module_help(self):
        for module in MODULES:
            with self.subTest(module=module):
                result = subprocess.run([sys.executable, "-B", "-m", f"tools.{module}", "--help"],
                    env={**os.environ, "NMRNET_IN_CONTAINER": "1"}, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("maintained Make target", result.stderr)


if __name__ == "__main__":
    unittest.main()
