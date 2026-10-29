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
from tools.wheelhouse import capture


def capture_inputs(root):
    """Freeze the wheels before verification so pip consumes the verified copies."""
    tests, lock = capture_tests(root)
    wheels = root / "wheels"
    wheels.mkdir()
    capture(lock, None, Path("/runtime-deps"), wheels)
    return wheels, tests, lock


def capture_tests(root, *, native_lock=None):
    """Keep the import witnesses independent of checkout and user-site imports."""
    tests = root / "tests"
    shutil.copytree("/native-tests", tests, symlinks=True)
    lock = tests / "runtime.lock"
    shutil.copyfile("/input/preparation.lock", lock)
    if native_lock is not None:
        shutil.copyfile(native_lock, tests / "native.lock")
    tools = tests / "tools"
    tools.mkdir()
    for name in ("container_guard.py", "artifacts.py", "wheelhouse.py"):
        shutil.copyfile(Path(__file__).with_name(name), tools / name)
    return tests, lock


def run_suite(python, root, tests, suite):
    if not (tests / f"test_{suite}.py").is_file():
        raise FileNotFoundError(f"Cannot run RDKit {suite} checks: the requested test module is missing.")
    subprocess.run(
        [str(python), "-I", "-B", "-m", "unittest", "discover", "-s", str(tests), "-p", f"test_{suite}.py", "-v"],
        cwd=root,
        check=True,
    )


def run_installed_tests(suite):
    sys.stdout.write(f"RDKit {suite} checks: Python {sys.version.split()[0]}, {sysconfig.get_platform()}\n")
    sys.stdout.flush()
    with tempfile.TemporaryDirectory(prefix="rdkit-", dir="/tmp") as temporary:
        root = Path(temporary)
        tests, _ = capture_tests(root, native_lock=Path("/opt/rdkit/provider-core-install.lock"))
        if suite in ("mol", "sdf", "geometry_worker", "smiles", "smiles_worker"):
            for name in ("nmrnet_provider", "nmrnet_inference"):
                shutil.copytree(Path("/workspace") / name, tests / name, symlinks=True)
            shutil.copytree("/geometry-fixtures", tests / "fixtures", symlinks=True)
        run_suite(sys.executable, root, tests, suite)


def run_tests():
    sys.stdout.write(f"RDKit dependency checks: Python {sys.version.split()[0]}, {sysconfig.get_platform()}\n")
    sys.stdout.flush()
    # Native extensions require executable mappings; the launcher grants only
    # this container-local tmpfs, retaining noexec on ordinary /tmp scratch.
    with tempfile.TemporaryDirectory(prefix="rdkit-", dir="/runtime") as temporary:
        root = Path(temporary)
        wheels, tests, lock = capture_inputs(root)
        python = install_runtime(root, wheels, lock)
        run_suite(python, root, tests, "runtime")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode",
        choices=("temporary", "runtime", "mol", "sdf", "geometry_worker", "smiles", "smiles_worker"),
        help="Install temporary dependencies, check the image, or test native readers, conformer generation and the reader channel.",
    )
    arguments = parser.parse_args()
    if arguments.mode == "temporary":
        run_tests()
    else:
        run_installed_tests(arguments.mode)
