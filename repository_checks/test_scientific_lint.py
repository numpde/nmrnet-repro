"""Execute both SECS CI convention gates with its exact Ruff tool version.

Source: secs-repro 34f5915c1534278bbf61b4c6592afa44cf94ab08,
secs/.github/workflows/ci.yml, nested secs 0414b5f9d65e61a6ba00aa046e24b8e48ce230b1.
The authored source inventory excludes the upstream submodule by ownership,
not a lint waiver. Missing Ruff is a failed dependency gate, never a skip.
https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/secs/.github/workflows/ci.yml
"""

from tools.container_guard import require_container

require_container()

import ast
import shutil
import subprocess
import unittest

from repository_checks.quality.sources import ROOT
from repository_checks.test_conventions import has_early_guard


def late_module_imports(source):
    """Allow the required early guard, then retain module import ordering.

    Ruff E402 cannot distinguish the mandatory guard from an arbitrary effect.
    Its per-file exception is paired with this scan: only a canonical early
    guard may precede the import section. Later effects cannot reopen it.
    """
    statements = ast.parse(source).body
    guarded = has_early_guard(source)
    imports_ended = False
    findings = []
    for index, statement in enumerate(statements):
        if (
            index == 0
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            if imports_ended:
                findings.append(statement.lineno)
            continue
        if (
            guarded
            and isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Call)
            and isinstance(statement.value.func, ast.Name)
            and statement.value.func.id == "require_container"
            and not imports_ended
        ):
            guarded = False
            continue
        imports_ended = True
    return findings


class ImportOrderDetectionTests(unittest.TestCase):
    def test_only_canonical_early_guard_may_precede_import_section(self):
        guard = "from tools.container_guard import require_container\nrequire_container()\n"
        self.assertEqual([], late_module_imports('"""Owner."""\n' + guard + "import pickle\nVALUE = 1\n"))
        self.assertEqual([4], late_module_imports(guard + "VALUE = 1\nimport pickle\n"))
        self.assertEqual([2, 4], late_module_imports("work()\n" + guard + "import pickle\n"))
        self.assertEqual([3], late_module_imports("import os\nwork()\nimport pickle\n"))
        self.assertEqual([2], late_module_imports("require_container()\nimport pickle\n"))
        self.assertEqual([4], late_module_imports(guard + "require_container()\nimport pickle\n"))


class ScientificLintTests(unittest.TestCase):
    def test_guard_exception_retains_late_module_import_rejection(self):
        findings = [
            f"{path.relative_to(ROOT)}:{line}: module import follows operational statements"
            for name in ("tools", "repository_checks", "tests", "nmrnet_inference", "nmrnet_provider")
            for path in sorted((ROOT / name).rglob("*.py"))
            for line in late_module_imports(path.read_text())
        ]
        self.assertEqual([], findings, msg="\n".join(findings))

    def run_ruff(self, operation):
        binary = shutil.which("ruff")
        self.assertIsNotNone(
            binary, msg="SECS lint/format gates require Ruff 0.15.15 in the maintained container; do not install on the host"
        )
        version = subprocess.run([binary, "--version"], check=True, capture_output=True, text=True)
        self.assertEqual("ruff 0.15.15", version.stdout.strip(), msg="The full source policy requires its pinned Ruff version")
        paths = [ROOT / name for name in ("tools", "repository_checks", "tests")]
        for path in paths:
            self.assertTrue(path.is_dir() and not path.is_symlink(), msg=f"Authored lint root is missing or a symlink: {path}")
            self.assertTrue(list(path.rglob("*.py")), msg=f"Authored lint inventory is empty: {path}")
        for name in ("nmrnet_inference", "nmrnet_provider"):
            package = ROOT / name
            if package.exists() or package.is_symlink():
                self.assertTrue(
                    package.is_dir() and not package.is_symlink(), msg=f"Authored package {name} must be a non-symlink directory"
                )
                self.assertTrue(list(package.rglob("*.py")), msg=f"Authored package {name} has no lintable sources")
                paths.append(package)
        arguments = [binary, operation, "--no-cache", "--config", str(ROOT / "repository_checks/lint/ruff.toml")]
        if operation == "format":
            arguments.append("--check")
        result = subprocess.run([*arguments, *(str(path) for path in paths)], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, msg=result.stdout + result.stderr)

    def test_full_selected_ruff_rule_policy(self):
        self.run_ruff("check")

    def test_ruff_format_policy(self):
        self.run_ruff("format")
