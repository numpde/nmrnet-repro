# Port source: https://github.com/numpde/vote-mcp/blob/6f595850d199e0d97098c1a7e67ddff774171a25/repo-checks/silent-defaults/test_env_file_empty_defaults.py
# Detector assertions adapted to NMRNet authored inputs; no private-file exemptions.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import _repo_surfaces
from ._repo_surfaces import (
    INTERPOLATION_DEFAULT_RE,
    repo_path,
    surface_files,
    surface_lines,
)

ENV_FILE_GLOBS = ("*.env", "*.env.example", "**/*.env", "**/*.env.example")
ENV_ASSIGNMENT_RE = re.compile(r"^(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.*)$")


def _env_files() -> list[Path]:
    return surface_files(ENV_FILE_GLOBS)


class EnvFileEmptyDefaultsTest(unittest.TestCase):
    maxDiff = None

    def test_env_assignment_matcher_flags_blank_assignments(self) -> None:
        match = ENV_ASSIGNMENT_RE.match("OCI_TOKEN=")
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual("", match.group("value"))
        self.assertEqual('""', ENV_ASSIGNMENT_RE.match('OCI_TOKEN=""').group("value"))
        self.assertEqual("''", ENV_ASSIGNMENT_RE.match("export OCI_TOKEN=''").group("value"))

    def test_env_interpolation_matcher_flags_default_expansion(self) -> None:
        self.assertRegex("API_ORIGIN=${API_ORIGIN:-https://example.test}", INTERPOLATION_DEFAULT_RE)

    def test_env_file_scan_includes_configurations_and_examples(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("provider.env", "provider.env.example"):
                (root / name).write_text("PROFILE=fixture\n")
            with patch.object(_repo_surfaces, "REPO_ROOT", root):
                scanned = {repo_path(path) for path in _env_files()}
            self.assertEqual({Path("provider.env"), Path("provider.env.example")}, scanned)

    def test_surface_files_always_excludes_silent_defaults_tests(self) -> None:
        scanned = {repo_path(path) for path in surface_files(("repository_checks/**/*.py",))}

        self.assertNotIn(
            Path("repository_checks/quality/silent_defaults/test_env_file_empty_defaults.py"),
            scanned,
        )

    def test_env_files_do_not_publish_blank_assignments(self) -> None:
        findings: list[str] = []
        for source, line_number, line in surface_lines(_env_files(), skip_comments=True):
            stripped = line.strip()
            if not stripped:
                continue
            match = ENV_ASSIGNMENT_RE.match(stripped)
            if match and match.group("value").strip() in {"", '""', "''"}:
                findings.append(f"{source}:{line_number}: {match.group('name')}={match.group('value')}")

        self.assertEqual([], findings)

    def test_env_files_do_not_use_interpolation_defaults(self) -> None:
        findings: list[str] = []
        for source, line_number, line in surface_lines(_env_files(), skip_comments=True):
            for match in INTERPOLATION_DEFAULT_RE.finditer(line):
                findings.append(
                    f"{source}:{line_number}: "
                    f"{match.group('name')}{match.group('operator')}{match.group('value')!r}: {line.strip()}"
                )

        self.assertEqual([], findings)


if __name__ == "__main__":
    unittest.main()
