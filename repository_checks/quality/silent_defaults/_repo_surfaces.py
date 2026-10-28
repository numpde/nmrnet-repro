# Port source: https://github.com/numpde/vote-mcp/blob/6f595850d199e0d97098c1a7e67ddff774171a25/repo-checks/silent-defaults/_repo_surfaces.py
# Source discovery and line classification adapted to NMRNet authored files.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import re
from collections.abc import Iterable, Iterator
from pathlib import Path

from ..sources import ROOT as REPO_ROOT

INTERPOLATION_DEFAULT_RE = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[#$!?*@_-])"
    r"(?P<operator>:?[-=])(?P<value>[^}]*)\}"
)
SILENT_DEFAULTS_TEST_PARTS = frozenset({"silent_defaults"})


def repo_path(path: Path) -> Path:
    return path.relative_to(REPO_ROOT)


def repo_source(path: Path) -> str:
    return repo_path(path).as_posix()


def surface_files(
    patterns: Iterable[str],
    *,
    extra_exclude_parts: frozenset[str] = frozenset(),
) -> list[Path]:
    exclude_parts = SILENT_DEFAULTS_TEST_PARTS | extra_exclude_parts
    paths: set[Path] = set()
    for pattern in patterns:
        paths.update(REPO_ROOT.glob(pattern))
    return sorted(
        path
        for path in paths
        if path.is_file() and not exclude_parts.intersection(repo_path(path).parts)
    )


def surface_lines(
    paths: Iterable[Path],
    *,
    skip_comments: bool = False,
) -> Iterator[tuple[str, int, str]]:
    for path in paths:
        source = repo_source(path)
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            if skip_comments and line.lstrip().startswith("#"):
                continue
            yield source, line_number, line
