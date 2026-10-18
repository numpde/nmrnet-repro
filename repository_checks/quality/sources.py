"""Inventory authored sources visible to the contained repository lane.

The launcher pins /workspace. NMRNetplusplus is the captured upstream source,
not authored tooling; local conventions govern tools, checks, tests and images.
Nested inference and provider sources enter these inventories automatically.
"""

from tools.container_guard import require_container

require_container()

from pathlib import Path

ROOT = Path("/workspace")
REPOSITORY_ROOT = ROOT
PYTHON_ROOTS = ("tools", "tests", "repository_checks")
OPTIONAL_PYTHON_ROOTS = ("nmrnet_inference", "nmrnet_provider")
SKIP_DIRS = {
    ".git",
    ".agents",
    ".codex",
    "NMRNetplusplus",
    "node_modules",
    "dependencies",
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def python_sources(*, production_only):
    """Use the caller's explicit production or whole-repository inventory.

    Both projections include nested inference and provider modules. Production
    checks additionally own tools; whole-repository checks own tools, tests and
    repository_checks. The caller must choose the check's responsibility.
    """
    # silent-defaults: allow-contract -- The required production_only choice selects tools or all authored roots; future inference modules belong to both inventories; proof=repository_checks/quality/test_source_inventory.py::SourceInventoryUnitTests.test_python_source_selection_respects_explicit_scope
    roots = ("tools",) if production_only else PYTHON_ROOTS
    return sorted(path for name in (*roots, *OPTIONAL_PYTHON_ROOTS) for path in (ROOT / name).rglob("*.py"))


def repo_path(path):
    return ROOT / path


def is_skipped(path):
    return any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)


def read_text(path):
    return path.read_text(encoding="utf-8")


def iter_repo_text_files():
    suffixes = {".py", ".sh", ".toml", ".json", ".txt", ".md", ".yaml", ".yml", ".in", ".lock"}
    names = {"Makefile", "Dockerfile", "Dockerfile.lock", ".dockerignore", ".gitignore"}
    return sorted(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not is_skipped(path) and (path.suffix in suffixes or path.name in names)
    )
