"""Run locked RDKit dependency checks in an isolated, temporary Python environment."""

from tools.container_guard import require_container

require_container()

import argparse
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from tools.wheelhouse import check as check_wheelhouse


def capture_inputs(root):
    """Freeze the wheels before verification so pip consumes the verified copies."""
    wheels = root / "wheels"
    shutil.copytree("/runtime-deps", wheels, symlinks=True)
    tests = root / "tests"
    shutil.copytree("/native-tests", tests, symlinks=True)
    lock = tests / "runtime.lock"
    shutil.copyfile("/input/preparation.lock", lock)
    check_wheelhouse(lock, None, wheels)
    tools = tests / "tools"
    tools.mkdir()
    for name in ("container_guard.py", "artifacts.py", "wheelhouse.py"):
        shutil.copyfile(Path(__file__).with_name(name), tools / name)
    return wheels, tests, lock


def install_runtime(root, wheels, lock):
    # Keep pip outside the runtime so its complete installed distribution set
    # must equal the applied lock, without a bootstrap-package exception list.
    # pip documents --python for environments created with --without-pip:
    # https://pip.pypa.io/en/stable/topics/python-option/
    runtime = root / "environment"
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(runtime)], check=True)
    python = runtime / "bin/python"
    pip = [sys.executable, "-I", "-m", "pip", "--isolated", "--python", str(python)]
    subprocess.run(
        [
            *pip,
            "install",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--no-index",
            "--only-binary=:all:",
            "--require-hashes",
            "--no-compile",
            "--find-links",
            str(wheels),
            "-r",
            str(lock),
        ],
        check=True,
    )
    subprocess.run([*pip, "--no-cache-dir", "check"], check=True)
    return python


def run_tests():
    sys.stdout.write(f"RDKit dependency checks: Python {sys.version.split()[0]}, {sysconfig.get_platform()}\n")
    sys.stdout.flush()
    # Native extensions require executable mappings; the launcher grants only
    # this container-local tmpfs, retaining noexec on ordinary /tmp scratch.
    with tempfile.TemporaryDirectory(prefix="rdkit-", dir="/runtime") as temporary:
        root = Path(temporary)
        wheels, tests, lock = capture_inputs(root)
        python = install_runtime(root, wheels, lock)
        subprocess.run(
            [str(python), "-I", "-B", "-m", "unittest", "discover", "-s", str(tests), "-p", "test_runtime.py", "-v"],
            cwd=root,
            check=True,
        )


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__).parse_args()
    run_tests()
