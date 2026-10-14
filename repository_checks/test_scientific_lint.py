"""Execute both SECS CI convention gates with its exact Ruff tool version.

Source: secs-repro 34f5915c1534278bbf61b4c6592afa44cf94ab08,
secs/.github/workflows/ci.yml, nested secs 0414b5f9d65e61a6ba00aa046e24b8e48ce230b1.
The authored source inventory excludes the upstream submodule by ownership,
not a lint waiver. Missing Ruff is a failed dependency gate, never a skip.
https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/secs/.github/workflows/ci.yml
"""

from tools.container_guard import require_container

require_container()

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ScientificLintTests(unittest.TestCase):
    def run_ruff(self, operation):
        binary = shutil.which("ruff")
        self.assertIsNotNone(binary, "SECS lint/format gates require Ruff 0.15.15 in the maintained container; do not install on the host")
        version = subprocess.run([binary, "--version"], check=True, capture_output=True, text=True)
        self.assertEqual(version.stdout.strip(), "ruff 0.15.15", "The full source policy requires its pinned Ruff version")
        paths = [ROOT / name for name in ("tools", "repository_checks", "tests")]
        for path in paths:
            self.assertTrue(path.is_dir() and not path.is_symlink(), f"Authored lint root is missing or a symlink: {path}")
            self.assertTrue(list(path.rglob("*.py")), f"Authored lint inventory is empty: {path}")
        package = ROOT / "nmrnet_inference"
        if package.exists() or package.is_symlink():
            self.assertTrue(package.is_dir() and not package.is_symlink(), "Authored inference package must be a non-symlink directory")
            self.assertTrue(list(package.rglob("*.py")), "Authored inference package has no lintable sources")
            paths.append(package)
        arguments = [binary, operation, "--no-cache", "--config", str(ROOT / "repository_checks/lint/ruff.toml")]
        if operation == "format":
            arguments.append("--check")
        result = subprocess.run([*arguments, *(str(path) for path in paths)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_full_selected_ruff_rule_policy(self):
        self.run_ruff("check")

    def test_ruff_format_policy(self):
        self.run_ruff("format")
