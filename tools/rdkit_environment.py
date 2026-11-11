"""Install the complete verified RDKit lock into an isolated environment offline."""

from tools.container_guard import require_container

require_container()

import argparse
import subprocess
from pathlib import Path

from acquisition_tool.installer.tools.installer import create_environment


def install_runtime(root, wheels, lock):
    runtime = root / "environment"
    python = create_environment(runtime)
    pip = [str(python), "-I", "-B", "-m", "pip", "--isolated"]
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
