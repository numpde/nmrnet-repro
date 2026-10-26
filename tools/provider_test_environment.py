"""Run consumer tests with the released core installed in an ephemeral environment."""

from tools.container_guard import require_container

require_container()

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from tools.provider_environment import install_provider
from tools.wheelhouse import capture


def run_suite(suite, arguments):
    # Native dependencies need executable mappings; all writable test state is
    # inside the caller's finite tmpfs, never in the source or shared cache.
    with tempfile.TemporaryDirectory(prefix="consumer-", dir="/runtime") as temporary:
        root = Path(temporary)
        lock = root / "install.lock"
        shutil.copyfile("/input/provider-core-install.lock", lock)
        wheels = root / "wheels"
        wheels.mkdir()
        capture(lock, None, Path("/wheelhouse"), wheels)
        python = install_provider(root / "provider", Path("/consumer"), lock, wheels)
        if suite == "integration":
            command = [str(python), "-I", "-B", "/workspace/tools/provider_consumer_tests.py", "run",
                       "--release", "/input/provider-core-release.json", "--pool", "/wheelhouse"]
            if arguments:
                command.extend(["--pattern", *arguments])
        else:
            command = [str(python), "-B", "-m", "unittest", *arguments]
        return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("unit", "integration"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    options = parser.parse_args()
    raise SystemExit(run_suite(options.suite, options.arguments))
