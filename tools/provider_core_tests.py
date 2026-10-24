"""Build and test the installed package using the existing offline container lane."""

from tools.container_guard import require_container

require_container()

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tools.wheelhouse import capture


def run_suite(suite, patterns):
    """Keep the source tree out of the runtime interpreter's import search path."""
    with tempfile.TemporaryDirectory(prefix="provider-core-") as temporary:
        root = Path(temporary)
        runtime_wheels = root / "runtime-wheels"
        build_wheels = root / "build-wheels"
        runtime_lock = root / "runtime.lock"
        build_lock = root / "build.lock"
        shutil.copyfile("/core-runtime.lock", runtime_lock)
        shutil.copyfile("/core-build.lock", build_lock)
        dependency_inputs = {}
        for role, lock, pool, destination in (
            ("runtime", runtime_lock, Path("/runtime-deps"), runtime_wheels),
            ("build", build_lock, Path("/build-deps"), build_wheels),
        ):
            destination.mkdir()
            dependency_inputs[role] = capture(lock, None, pool, destination)
        sys.stdout.write("Captured provider-core dependency inputs: " + json.dumps(dependency_inputs, sort_keys=True) + "\n")
        sys.stdout.flush()
        source = Path("/package")
        for path in source.rglob("*"):
            if path.is_symlink():
                raise ValueError("Cannot build provider-core: package inputs must not contain symlinks")
        build_source = root / "source"
        build_source.mkdir()
        shutil.copyfile(source / "pyproject.toml", build_source / "pyproject.toml")
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
                str(build_wheels),
                "-r",
                str(build_lock),
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
                str(runtime_wheels),
                "-r",
                str(runtime_lock),
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
                *patterns,
            ],
            cwd=root / "run",
            check=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "suite",
        choices=(
            "unit",
            "unit/normative",
            "unit/adversarial",
            "contract",
            "integration",
            "integration/chat",
            "integration/hello",
            "integration/transport",
            "integration/api",
            "integration/delivery",
            "integration/starting",
        ),
    )
    parser.add_argument("--pattern", nargs="+", default=["test*.py"], help="Select focused unittest discovery patterns.")
    arguments = parser.parse_args()
    run_suite(arguments.suite, arguments.pattern)
