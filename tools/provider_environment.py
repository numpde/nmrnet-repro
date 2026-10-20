"""Build core and install the controller offline, leaving a pip-free runtime.

Ports tools/provider_core_tests.py's locked wheel build and
tools/rdkit_environment.py's external-pip installation at 9c0d9b0. This build-only
owner consumes frozen contexts; runtime startup performs no installation.
"""

from tools.container_guard import require_container

require_container()

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from tools.wheelhouse import check, inventory


def install_provider(root, source, build_wheels, runtime_wheels):
    """Verify both dependency sets before creating environments or executing pip."""
    build_lock = source / "requirements/provider-core-build.lock"
    runtime_lock = source / "requirements/provider-core.lock"
    build_identity = inventory(build_lock, None, build_wheels)
    check(runtime_lock, None, runtime_wheels)
    runtime_identity = inventory(runtime_lock, None, runtime_wheels)
    root.mkdir(mode=0o755)
    runtime = root / "environment"
    pip = [sys.executable, "-I", "-m", "pip", "--isolated"]
    with tempfile.TemporaryDirectory(prefix="provider-build-", dir=source) as temporary:
        builder = Path(temporary) / "builder"
        for environment, wheels, lock in ((builder, build_wheels, build_lock), (runtime, runtime_wheels, runtime_lock)):
            subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(environment)], check=True)
            subprocess.run(
                [
                    *pip,
                    "--python",
                    str(environment / "bin/python"),
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
        output = Path(temporary) / "wheels"
        subprocess.run(
            [
                *pip,
                "--python",
                str(builder / "bin/python"),
                "wheel",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-index",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(output),
                str(source / "packages/provider-core"),
            ],
            check=True,
        )
        built = list(output.glob("provider_core-*.whl"))
        if len(built) != 1:
            raise ValueError("Cannot install provider controller: core build must produce exactly one wheel.")
        subprocess.run(
            [
                *pip,
                "--python",
                str(runtime / "bin/python"),
                "install",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-index",
                "--no-deps",
                "--no-compile",
                str(built[0]),
            ],
            check=True,
        )
        subprocess.run([*pip, "--python", str(runtime / "bin/python"), "--no-cache-dir", "check"], check=True)
    # Install binding files beside the wheel, so -I excludes ambient paths while
    # still finding image-owned code. The binding is not a second distribution.
    location = subprocess.check_output(
        [
            str(runtime / "bin/python"),
            "-I",
            "-B",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        text=True,
    ).strip()
    installed = Path(location)
    for name in ("nmrnet_provider", "nmrnet_inference"):
        shutil.copytree(source / name, installed / name)
    support = installed / "tools"
    support.mkdir()
    for name in ("container_guard.py", "artifacts.py"):
        shutil.copyfile(source / "tools" / name, support / name)
    shutil.copyfile(runtime_lock, root / "provider-core.lock")
    (root / "dependency-inputs.json").write_text(
        json.dumps({"build": build_identity, "runtime": runtime_identity}, sort_keys=True) + "\n"
    )
    return runtime / "bin/python"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "source", "build-wheels", "runtime-wheels"):
        parser.add_argument("--" + name, type=Path, required=True)
    options = parser.parse_args()
    install_provider(options.root, options.source, options.build_wheels, options.runtime_wheels)
