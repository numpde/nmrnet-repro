"""Install the complete verified RDKit lock into a pip-free environment offline."""

from tools.container_guard import require_container

require_container()

import argparse
import subprocess
import sys
from pathlib import Path

from tools.wheelhouse import check as check_wheelhouse


def install_runtime(root, wheels, lock):
    # External pip keeps the installed distribution set equal to the lock.
    # https://pip.pypa.io/en/stable/topics/python-option/
    check_wheelhouse(lock, None, wheels)
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--wheels", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    arguments = parser.parse_args()
    install_runtime(arguments.root, arguments.wheels, arguments.lock)
