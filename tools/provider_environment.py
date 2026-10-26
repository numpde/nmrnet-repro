"""Install the released provider core and NMRNet bindings without building core.

The shared owner supplies the wheel and complete installation lock. This
consumer combines that closure with each existing scientific environment;
it never builds core or resolves replacement scientific dependencies.
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


def _install_core(python, lock, runtime_wheels, evidence_root):
    installed = json.loads(subprocess.check_output(
        [str(python), "-I", "-B", "-c",
         "import importlib.metadata,json,re; print(json.dumps({re.sub(r'[-_.]+','-',d.metadata['Name']).lower():d.version for d in importlib.metadata.distributions()}))"],
        text=True,
    ))
    for name, version in requirements(lock):
        if name in installed and installed[name] != version:
            raise ValueError(
                f"Cannot extend interpreter: installed {name}=={installed[name]} differs from core lock {version}; review the locks before changing installed versions."
            )
    pip = [sys.executable, "-I", "-m", "pip", "--isolated", "--python", str(python)]
    with tempfile.TemporaryDirectory(prefix="provider-install-") as temporary:
        report = Path(temporary) / "runtime-install.json"
        subprocess.run(
            [*pip, "install", "--ignore-installed", "--disable-pip-version-check", "--no-cache-dir",
             "--no-index", "--no-deps", "--only-binary=:all:", "--require-hashes", "--no-compile",
             "--report", str(report), "--find-links", str(runtime_wheels), "-r", str(lock)],
            check=True,
        )
        identities = {"runtime": wheel_inputs_from_report(lock, runtime_wheels, report)}
        subprocess.run([*pip, "--no-cache-dir", "check"], check=True)
    shutil.copyfile(lock, evidence_root / "provider-core-install.lock")
    (evidence_root / "dependency-inputs.json").write_text(json.dumps(identities, sort_keys=True) + "\n")


def _install_binding(python, source):
    # Bindings live beside the core wheel so -I excludes ambient source while
    # still finding image-owned code. They are not a second distribution.
    location = subprocess.check_output(
        [str(python), "-I", "-B", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
    ).strip()
    installed = Path(location)
    for name in ("nmrnet_provider", "nmrnet_inference"):
        shutil.copytree(source / name, installed / name)
    support = installed / "tools"
    support.mkdir()
    for name in ("container_guard.py", "artifacts.py", "baseline_prepare.py", "container_resources.py"):
        shutil.copyfile(source / "tools" / name, support / name)


def install_provider(root, source, install_lock, runtime_wheels):
    root.mkdir(mode=0o755)
    python = root / "environment/bin/python"
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(python.parent.parent)], check=True)
    _install_core(python, install_lock, runtime_wheels, root)
    _install_binding(python, source)
    return python


def install_provider_into(python, source, install_lock, runtime_wheels, evidence_root):
    """Keep scientific versions unchanged while adding the pinned core closure."""
    _install_core(python, install_lock, runtime_wheels, evidence_root)
    _install_binding(python, source)
    return python


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "source", "install-lock", "runtime-wheels"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--python", type=Path, help="Extend this existing scientific interpreter; otherwise create the controller environment.")
    options = parser.parse_args()
    if options.python is None:
        install_provider(options.root, options.source, options.install_lock, options.runtime_wheels)
    else:
        install_provider_into(options.python, options.source, options.install_lock, options.runtime_wheels, options.root)
