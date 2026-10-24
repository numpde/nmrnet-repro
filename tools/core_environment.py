"""Build and install provider-core offline, leaving a pip-free runtime.

Ports tools/provider_core_tests.py's locked wheel build and
tools/rdkit_environment.py's external-pip installation at 9c0d9b0. This build-only
owner consumes frozen contexts; runtime startup performs no installation.
Extracted from nmrnet-repro58874e34d1886cfc0f277deadaec3b05d1f2fc30,
tools/provider_environment.py::install_provider/install_provider_into.
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

from tools.wheelhouse import requirements, wheel_inputs_from_report


def install_core(root, source, build_wheels, runtime_wheels):
    """Create a core-only environment without installing any provider binding."""
    root.mkdir(mode=0o755)
    python = root / "environment/bin/python"
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(python.parent.parent)], check=True)
    return install_core_into(python, source, build_wheels, runtime_wheels, root)


def install_core_into(python, source, build_wheels, runtime_wheels, evidence_root):
    """Extend an existing interpreter without replacing its scientific environment.

    Controller, RDKit and inference builds share the complete pinned core closure.
    Scientific versions stay owned by their separate locks; pip check rejects a
    conflicting union rather than resolving/upgrading those scientific packages.
    """
    build_lock = source / "requirements/provider-core-build.lock"
    runtime_lock = source / "requirements/provider-core.lock"
    installed = json.loads(subprocess.check_output(
        [str(python), "-I", "-B", "-c",
         "import importlib.metadata,json,re; print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower():d.version for d in importlib.metadata.distributions()}))"],
        text=True,
    ))
    for name, version in requirements(runtime_lock):
        if name in installed and installed[name] != version:
            raise ValueError(
                f"Cannot extend interpreter: installed {name}=={installed[name]} differs from core lock {version}; review the locks before changing installed versions."
            )
    identities = {}
    pip = [sys.executable, "-I", "-m", "pip", "--isolated"]
    with tempfile.TemporaryDirectory(prefix="provider-build-", dir=source) as temporary:
        builder = Path(temporary) / "builder"
        subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(builder)], check=True)
        for role, target, wheels, lock in (
            ("build", builder / "bin/python", build_wheels, build_lock),
            ("runtime", python, runtime_wheels, runtime_lock),
        ):
            report = Path(temporary) / f"{role}-install.json"
            subprocess.run(
                [
                    *pip,
                    "--python",
                    str(target),
                    "install",
                    "--ignore-installed",
                    "--disable-pip-version-check",
                    "--no-cache-dir",
                    "--no-index",
                    "--only-binary=:all:",
                    "--require-hashes",
                    "--no-compile",
                    "--report",
                    str(report),
                    "--find-links",
                    str(wheels),
                    "-r",
                    str(lock),
                ],
                check=True,
            )
            identities[role] = wheel_inputs_from_report(lock, wheels, report)
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
            raise ValueError("Cannot install provider-core: core build must produce exactly one wheel.")
        subprocess.run(
            [
                *pip,
                "--python",
                str(python),
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
        subprocess.run([*pip, "--python", str(python), "--no-cache-dir", "check"], check=True)
    shutil.copyfile(runtime_lock, evidence_root / "provider-core.lock")
    (evidence_root / "dependency-inputs.json").write_text(json.dumps(identities, sort_keys=True) + "\n")
    return python


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "source", "build-wheels", "runtime-wheels"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--python", type=Path, help="Extend an existing interpreter; otherwise create the core environment.")
    options = parser.parse_args()
    if options.python is None:
        install_core(options.root, options.source, options.build_wheels, options.runtime_wheels)
    else:
        install_core_into(options.python, options.source, options.build_wheels, options.runtime_wheels, options.root)
