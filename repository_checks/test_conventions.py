"""Check repository conventions against authored sources and maintained targets.

NMRPeak supplies the hierarchical test aggregate; Magnet supplies recursive
production-source inventories with narrow, documented ownership exceptions:
https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/Makefile#L87-L102
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_provider_event_logging_posture.py#L34-L94
This suite inspects source without importing scientific dependencies. Its own
execution remains inside the maintained container, just like the unit suite.
"""

from tools.container_guard import require_container

require_container()

import ast
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def has_early_guard(source, *, package_marker=False):
    """Recognize the canonical guard before imports or executable statements.

    Empty/docstring-only package markers are a local exception: importing them
    cannot start work. An initializer with imports or other statements must guard.
    """
    statements = ast.parse(source).body
    if statements and isinstance(statements[0], ast.Expr) and isinstance(statements[0].value, ast.Constant) and isinstance(statements[0].value.value, str):
        statements = statements[1:]
    if package_marker and not statements:
        return True
    while (statements and isinstance(statements[0], ast.ImportFrom)
           and statements[0].level == 0 and statements[0].module == "__future__"):
        statements = statements[1:]
    if len(statements) < 2:
        return False
    imported, called = statements[:2]
    return (isinstance(imported, ast.ImportFrom) and imported.level == 0
            and imported.module == "tools.container_guard" and len(imported.names) == 1
            and imported.names[0].name == "require_container" and imported.names[0].asname is None
            and isinstance(called, ast.Expr) and isinstance(called.value, ast.Call)
            and isinstance(called.value.func, ast.Name) and called.value.func.id == "require_container"
            and not called.value.args and not called.value.keywords)


def unguarded_sources(root):
    """Include new production files automatically, without scanning upstream or fixtures."""
    sources = {path for name in ("tools", "repository_checks", "nmrnet_inference")
               for path in (root / name).rglob("*.py")}
    # The shared guard must be importable before its caller checks the marker.
    sources.discard(root / "tools" / "container_guard.py")
    return [str(path.relative_to(root)) for path in sorted(sources)
            if not has_early_guard(path.read_text(), package_marker=path.name == "__init__.py")]


def make_targets(source):
    """Read literal root-Makefile targets; recipes and variable assignments are excluded."""
    targets = {}
    for line in source.splitlines():
        match = re.match(r"^([A-Za-z0-9_./% -]+):(?!=)(.*)$", line)
        if match and not match[1].startswith("."):
            for name in match[1].split():
                targets[name] = match[2].split("#", 1)[0].split()
    return targets


class RepositoryConventionTests(unittest.TestCase):
    def test_authored_production_sources_require_the_shared_guard(self):
        self.assertTrue(list((ROOT / "tools").rglob("*.py")), "production source inventory is empty")
        self.assertEqual(unguarded_sources(ROOT), [])

    def test_public_make_targets_are_hierarchical_and_test_aggregates_both_suites(self):
        targets = make_targets((ROOT / "Makefile").read_text())
        self.assertTrue(targets, "Makefile target inventory is empty")
        for name in targets:
            if name not in ("test", "help"):
                self.assertRegex(name, r"^[a-z][a-z0-9_-]*(/[a-z][a-z0-9_-]*)+$")
        self.assertTrue({"test/unit", "test/repository"} <= targets.keys())
        self.assertTrue({"test/unit", "test/repository"} <= set(targets["test"]))


class ConventionDetectionTests(unittest.TestCase):
    GUARD = "from tools.container_guard import require_container\nrequire_container()\n"

    def test_missing_late_conditional_and_rebound_guards_fail(self):
        for source in ("import pickle\n", "import pickle\n" + self.GUARD,
                       "from .__future__ import annotations\n" + self.GUARD,
                       "if True:\n    from tools.container_guard import require_container\n    require_container()\n",
                       "from tools.container_guard import require_container\nrequire_container = lambda: None\nrequire_container()\n"):
            with self.subTest(source=source):
                self.assertFalse(has_early_guard(source))

    def test_canonical_guard_allows_module_documentation_and_future_import(self):
        source = '"""Worker responsibility."""\nfrom __future__ import annotations\n' + self.GUARD + "import pickle\n"
        self.assertTrue(has_early_guard(source))

    def test_only_empty_or_documentation_package_markers_are_exempt(self):
        for source in ("", '"""Package marker."""\n'):
            self.assertTrue(has_early_guard(source, package_marker=True))
            self.assertFalse(has_early_guard(source))
        self.assertFalse(has_early_guard("import pickle\n", package_marker=True))
        self.assertTrue(has_early_guard(self.GUARD + "import pickle\n", package_marker=True))

    def test_new_nested_workers_and_future_package_enter_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("tools/container_guard.py", "tools/new/worker.py", "nmrnet_inference/model.py",
                         "repository_checks/nested/test_new.py"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("import pickle\n")
            self.assertEqual(unguarded_sources(root), ["nmrnet_inference/model.py", "repository_checks/nested/test_new.py", "tools/new/worker.py"])
            (root / "tools/new/worker.py").write_text(self.GUARD)
            self.assertEqual(unguarded_sources(root), ["nmrnet_inference/model.py", "repository_checks/nested/test_new.py"])

    def test_target_parser_ignores_recipes_and_assignments(self):
        targets = make_targets(".PHONY: test test/unit\nROOT := /workspace\ntest: test/unit test/repository\n\techo unexpected: text\ntest/unit test/repository:\n")
        self.assertEqual(targets, {"test": ["test/unit", "test/repository"], "test/unit": [], "test/repository": []})

    def test_repository_lane_grants_only_reviewed_sources_including_future_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            launcher = root / "tools/container.sh"
            launcher.write_bytes((ROOT / "tools/container.sh").read_bytes())
            package = root / "nmrnet_inference"
            for include_package in (False, True):
                if include_package:
                    package.mkdir()
                result = subprocess.run(["bash", str(launcher), "--print", "test/repository"],
                                        text=True, capture_output=True, check=True)
                arguments = shlex.split(result.stdout)
                mounts = {arguments[index + 1] for index, value in enumerate(arguments) if value == "--mount"}
                names = {"tools", "repository_checks", "Makefile"}
                if include_package:
                    names.add("nmrnet_inference")
                self.assertEqual(mounts, {f"type=bind,src={root / name},dst=/workspace/{name},readonly" for name in names})
                self.assertEqual(arguments[arguments.index("--network") + 1], "none")
                self.assertEqual(arguments[arguments.index("--pull") + 1], "never")
                self.assertIn("--read-only", arguments)
                self.assertIn("NMRNET_IN_CONTAINER=1", arguments)
            package.rmdir()
            package.symlink_to(root / "elsewhere")
            result = subprocess.run(["bash", str(launcher), "--print", "test/repository"],
                                    text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
