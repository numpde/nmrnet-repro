"""Install NMRNet bindings after the shared locked core environment build.

The core-only delivery image uses tools.core_environment directly. Scientific
and controller images retain this owner for NMRNet modules and worker support.
Source of the shared build: nmrnet-repro58874e34d1886cfc0f277deadaec3b05d1f2fc30,
tools/provider_environment.py::install_provider/install_provider_into.
"""

from tools.container_guard import require_container

require_container()

import argparse
import shutil
import subprocess
from pathlib import Path

from tools.core_environment import install_core, install_core_into


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
    for name in ("container_guard.py", "artifacts.py", "provider_proxy.py", "bound_http_proxy.py", "baseline_prepare.py", "container_resources.py"):
        shutil.copyfile(source / "tools" / name, support / name)


def install_provider(root, source, build_wheels, runtime_wheels):
    python = install_core(root, source, build_wheels, runtime_wheels)
    _install_binding(python, source)
    return python


def install_provider_into(python, source, build_wheels, runtime_wheels, evidence_root):
    """Keep scientific versions unchanged while adding the pinned core closure."""
    install_core_into(python, source, build_wheels, runtime_wheels, evidence_root)
    _install_binding(python, source)
    return python


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "source", "build-wheels", "runtime-wheels"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--python", type=Path, help="Extend this existing scientific interpreter; otherwise create the controller environment.")
    options = parser.parse_args()
    if options.python is None:
        install_provider(options.root, options.source, options.build_wheels, options.runtime_wheels)
    else:
        install_provider_into(options.python, options.source, options.build_wheels, options.runtime_wheels, options.root)
