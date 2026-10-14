# Port source: https://github.com/numpde/nmr-api-v1/blob/dbd81a81ee92bd04d3223f4389a26891181cc0cd/repository_checks/test_runtime_policy_posture.py
# Original detector fixtures and repository assertions retained; source roots alone are local.
"""Flag module-scope uppercase direct-name value assignments for review."""

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
import textwrap
import unittest

from .sources import REPOSITORY_ROOT, python_sources

_NMR_API_ROOT = REPOSITORY_ROOT / "nmr_api"
_ESCAPE = "# repo-check: allow-module-constant"


@dataclass(frozen=True, slots=True)
class _Finding:
    path: Path
    line: int
    name: str

    def diagnostic(self) -> str:
        return (
            f"{self.path}:{self.line}: module-scope uppercase direct-name value "
            f"assignment {self.name}; review whether it belongs in a settings "
            "snapshot; a retained single-name assignment must place "
            f"{_ESCAPE!r} immediately above it"
        )


def _is_uppercase_name(name: str) -> bool:
    return any(character.isalpha() for character in name) and name.upper() == name


def _binding_names(binding: ast.Assign | ast.AnnAssign) -> list[str]:
    if isinstance(binding, ast.Assign):
        return [
            target.id for target in binding.targets if isinstance(target, ast.Name)
        ]
    if isinstance(binding, ast.AnnAssign) and isinstance(binding.target, ast.Name):
        return [binding.target.id]
    return []


class _ModuleBindingCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.bindings: list[ast.Assign | ast.AnnAssign] = []

    def visit_Assign(self, node: ast.Assign) -> None:
        self.bindings.append(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.bindings.append(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return


def _module_bindings(tree: ast.Module) -> list[ast.Assign | ast.AnnAssign]:
    collector = _ModuleBindingCollector()
    collector.visit(tree)
    return sorted(collector.bindings, key=lambda binding: binding.lineno)


def _has_escape(binding: ast.Assign | ast.AnnAssign, source_lines: list[str]) -> bool:
    if binding.lineno < 2:
        return False
    if isinstance(binding, ast.Assign) and (
        len(binding.targets) != 1 or not isinstance(binding.targets[0], ast.Name)
    ):
        return False
    preceding_line = source_lines[binding.lineno - 2]
    indentation = preceding_line[: len(preceding_line) - len(preceding_line.lstrip())]
    return len(indentation) == binding.col_offset and preceding_line.strip() == _ESCAPE


def _is_historical_migration(path: Path) -> bool:
    """Recognize only numbered Django migrations in an NMR API app."""

    if (
        len(path.parts) < 4
        or path.parts[0] != "nmr_api"
        or path.parent.name != "migrations"
        or path.suffix != ".py"
    ):
        return False
    sequence, separator, name = path.stem.partition("_")
    return (
        separator == "_"
        and sequence.isascii()
        and sequence.isdigit()
        and name.isidentifier()
    )


def _findings(path: Path, source: str) -> list[_Finding]:
    if _is_historical_migration(path):
        return []
    tree = ast.parse(source, filename=str(path))
    source_lines = source.splitlines()
    return [
        _Finding(path=path, line=binding.lineno, name=name)
        for binding in _module_bindings(tree)
        if not _has_escape(binding, source_lines)
        for name in _binding_names(binding)
        if _is_uppercase_name(name)
    ]


def _scan(paths: Iterable[Path]) -> list[_Finding]:
    return sorted(
        [
            finding
            for path in paths
            for finding in _findings(
                path.relative_to(REPOSITORY_ROOT),
                path.read_text(encoding="utf-8"),
            )
        ],
        key=lambda finding: (finding.path, finding.line, finding.name),
    )


class RuntimePolicyPostureTests(unittest.TestCase):
    def test_detector_flags_module_scope_uppercase_direct_name_value_assignments(
        self,
    ) -> None:
        source = textwrap.dedent(
            """
            DIRECT = 1
            ANNOTATED: int = 2
            DECLARATION_ONLY: int

            if enabled:
                CONDITIONAL = load_value()

            lower_case = 3

            class Owner:
                CLASS_VALUE = 4

            def operation():
                LOCAL_VALUE = 5
            """
        )

        self.assertEqual(
            ["DIRECT", "ANNOTATED", "CONDITIONAL"],
            [finding.name for finding in _findings(Path("example.py"), source)],
        )

    def test_exact_local_escape_suppresses_single_name_assignment(self) -> None:
        source = textwrap.dedent(
            """
            # repo-check: allow-module-constant
            STABLE_CONTRACT_FACT = "v1"
            RUNTIME_LIMIT = 10
            """
        )

        self.assertEqual(
            ["RUNTIME_LIMIT"],
            [finding.name for finding in _findings(Path("example.py"), source)],
        )

    def test_escape_near_matches_remain_findings(self) -> None:
        cases = (
            (
                "trailing explanation",
                "# repo-check: allow-module-constant because this is stable\n"
                "VALUE = 1\n",
                ["VALUE"],
            ),
            (
                "blank line",
                "# repo-check: allow-module-constant\n\nVALUE = 1\n",
                ["VALUE"],
            ),
            (
                "wrong indentation",
                "if enabled:\n"
                "# repo-check: allow-module-constant\n"
                "    VALUE = 1\n",
                ["VALUE"],
            ),
            (
                "chained assignment",
                "# repo-check: allow-module-constant\nFIRST = SECOND = 1\n",
                ["FIRST", "SECOND"],
            ),
        )

        for name, source, expected_names in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    expected_names,
                    [
                        finding.name
                        for finding in _findings(Path("example.py"), source)
                    ],
                )

    def test_historical_migration_path_grammar(self) -> None:
        accepted = (
            Path("nmr_api/example/migrations/0001_initial.py"),
            Path("nmr_api/security/persistence/migrations/0007_action.py"),
            Path("nmr_api/example/migrations/10000_followup.py"),
        )
        rejected = (
            Path("nmr_api/example/migrations/runtime.py"),
            Path("nmr_api/example/migrations/0001.py"),
            Path("nmr_api/example/migrations/0001_.py"),
            Path("nmr_api/example/migrations/00a1_initial.py"),
            Path("nmr_api/example/migrations/٠٠٠١_initial.py"),
            Path("nmr_api/example/migrations/0001_-.py"),
            Path("nmr_api/example/migrations/0001_initial.txt"),
            Path("nmr_api/example/migrations/0001_initial.py.bak"),
            Path("nmr_api/example/migrations/archive/0001_initial.py"),
            Path("nmr_api/example/not-migrations/0001_initial.py"),
            Path("repository_checks/migrations/0001_initial.py"),
        )

        for path in accepted:
            with self.subTest(path=path):
                self.assertTrue(_is_historical_migration(path), msg=str(path))
        for path in rejected:
            with self.subTest(path=path):
                self.assertFalse(_is_historical_migration(path), msg=str(path))

    def test_diagnostic_does_not_disclose_assigned_values(self) -> None:
        finding = _findings(
            Path("example.py"),
            'SECRET_KEY = "fixture-secret"\n',
        )[0]

        self.assertNotIn("fixture-secret", finding.diagnostic())

    def test_detector_ignores_historical_migrations(self) -> None:
        self.assertEqual(
            [],
            _findings(
                Path("nmr_api/example/migrations/0001_initial.py"),
                "RUNTIME_CAPACITY = 4\n",
            ),
        )

    def test_migration_exemption_does_not_hide_nonmigration_python(self) -> None:
        self.assertEqual(
            ["RUNTIME_CAPACITY"],
            [
                finding.name
                for finding in _findings(
                    Path("nmr_api/example/migrations/runtime.py"),
                    "RUNTIME_CAPACITY = 4\n",
                )
            ],
        )

    def test_report_module_scope_uppercase_direct_name_value_assignments(
        self,
    ) -> None:
        findings = _scan(python_sources(production_only=True))
        if findings:
            self.fail(
                "module-scope uppercase direct-name value assignment review "
                "triggers:\n"
                + "\n".join(finding.diagnostic() for finding in findings)
            )
