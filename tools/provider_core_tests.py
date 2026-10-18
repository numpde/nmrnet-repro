"""Build and test the installed package using the existing offline container lane."""

from tools.container_guard import require_container

require_container()

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tools.wheelhouse import check as check_wheelhouse


def run_suite(suite, pattern):
    """Keep the source tree out of the runtime interpreter's import search path."""
    check_wheelhouse(Path("/core-runtime.lock"), None, Path("/runtime-deps"))
    with tempfile.TemporaryDirectory(prefix="provider-core-") as temporary:
        root = Path(temporary)
        source = Path("/package")
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError("Cannot build provider-core: package inputs must not contain symlinks")
        build_source = root / "source"
        build_source.mkdir()
        for name in ("pyproject.toml", "README.md"):
            shutil.copyfile(source / name, build_source / name)
        shutil.copytree(source / "src", build_source / "src")
        shutil.copytree(source / "tests", root / "run" / "tests")
        if suite == "integration":
            shutil.copytree(Path("/consumer-tests"), root / "run/tests/integration/nmrnet")
        # Only the runtime needs executable mappings for cryptography's native
        # extension; source/build scratch stays on the lane's noexec /tmp.
        runtime_root = Path("/runtime")
        for environment in (root / "build", runtime_root):
            subprocess.run([sys.executable, "-I", "-m", "venv", str(environment)], check=True)
        builder = str(root / "build/bin/python")
        runtime = str(runtime_root / "bin/python")
        subprocess.run(
            [
                builder,
                "-I",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-index",
                "--no-deps",
                "--require-hashes",
                "--find-links",
                "/build-deps",
                "-r",
                "/core-build.lock",
            ],
            check=True,
        )
        subprocess.run(
            [
                builder,
                "-I",
                "-m",
                "pip",
                "wheel",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-index",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(root / "dist"),
                str(build_source),
            ],
            check=True,
        )
        wheels = list((root / "dist").glob("provider_core-*.whl"))
        if len(wheels) != 1:
            raise ValueError("Cannot test provider-core: build must produce exactly one wheel")
        subprocess.run(
            [
                runtime,
                "-I",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-index",
                "--only-binary=:all:",
                "--require-hashes",
                "--no-compile",
                "--find-links",
                "/runtime-deps",
                "-r",
                "/core-runtime.lock",
            ],
            check=True,
        )
        subprocess.run(
            [
                runtime,
                "-I",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-index",
                "--no-deps",
                "--no-compile",
                str(wheels[0]),
            ],
            check=True,
        )
        subprocess.run([runtime, "-I", "-m", "pip", "--no-cache-dir", "check"], check=True)
        subprocess.run(
            [
                runtime,
                "-I",
                "-B",
                str(root / "run/tests/run_suite.py"),
                suite,
                pattern,
            ],
            cwd=root / "run",
            check=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("unit", "unit/normative", "unit/adversarial", "contract", "integration"))
    parser.add_argument("--pattern", default="test*.py", help="Select a focused unittest discovery pattern.")
    arguments = parser.parse_args()
    run_suite(arguments.suite, arguments.pattern)
