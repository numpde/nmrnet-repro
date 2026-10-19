"""Check locked RDKit imports in a temporary environment or recorded worker image."""

from tools.container_guard import require_container

require_container()

import argparse
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

from tools.rdkit_environment import install_runtime


def capture_inputs(root):
    """Freeze the wheels before verification so pip consumes the verified copies."""
    wheels = root / "wheels"
    shutil.copytree("/runtime-deps", wheels, symlinks=True)
    tests, lock = capture_tests(root)
    return wheels, tests, lock


def capture_tests(root):
    """Keep the import witnesses independent of checkout and user-site imports."""
    tests = root / "tests"
    shutil.copytree("/native-tests", tests, symlinks=True)
    lock = tests / "runtime.lock"
    shutil.copyfile("/input/preparation.lock", lock)
    tools = tests / "tools"
    tools.mkdir()
    for name in ("container_guard.py", "artifacts.py", "wheelhouse.py"):
        shutil.copyfile(Path(__file__).with_name(name), tools / name)
    return tests, lock


def run_installed_tests(suite):
    sys.stdout.write(f"RDKit {suite} checks: Python {sys.version.split()[0]}, {sysconfig.get_platform()}\n")
    sys.stdout.flush()
    with tempfile.TemporaryDirectory(prefix="rdkit-", dir="/tmp") as temporary:
        root = Path(temporary)
        tests, _ = capture_tests(root)
        if suite == "mol":
            for name in ("nmrnet_provider", "nmrnet_inference"):
                shutil.copytree(Path("/workspace") / name, tests / name, symlinks=True)
            shutil.copytree("/geometry-fixtures", tests / "fixtures", symlinks=True)
        subprocess.run(
            [sys.executable, "-I", "-B", "-m", "unittest", "discover", "-s", str(tests), "-p", f"test_{suite}.py", "-v"],
            cwd=root,
            check=True,
        )


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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("temporary", "runtime", "mol"),
        help="Install temporary dependencies, check the image, or test MOL reading.",
    )
    arguments = parser.parse_args()
    if arguments.mode == "temporary":
        run_tests()
    else:
        run_installed_tests(arguments.mode)
