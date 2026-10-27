"""Run NMRNet consumer tests against the installed shared release.

The release source archive supplies reusable test fakes only. Core source and
generic test cases never enter the consumer import or discovery paths.
"""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import importlib.metadata
import json
import os
import runpy
import shutil
import sys
import tarfile
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path

from tools.artifacts import regular_file


SUPPORT_MODULES = (
    "acquisition", "api", "api_tls", "attempt_inventory", "attempt_store", "attempts",
    "checkpoint_execution", "condition_recovery", "conditions", "controller", "conversation",
    "credential", "delivery", "execution", "hello", "job_feed", "job_input", "runner",
    "tls_peer", "transport", "unix_proxy", "uploads", "live_analysis",
)


def copy_support(source, expected_sha256, root, version):
    """Hash the captured archive before selecting its exact regular support files."""
    captured = root / "core-source.tar.gz"
    checksum = hashlib.sha256()
    with regular_file(source, os.O_RDONLY) as stream, captured.open("xb") as target:
        while chunk := stream.read(1024 * 1024):
            target.write(chunk)
            checksum.update(chunk)
    if checksum.hexdigest() != expected_sha256:
        raise ValueError("Cannot prepare consumer tests: shared source archive differs from its pinned SHA-256.")
    names = ("tests/__init__.py", "tests/integration/__init__.py", "tests/fakes/__init__.py",
             *(f"tests/fakes/{name}.py" for name in SUPPORT_MODULES))
    with tarfile.open(captured, "r:gz") as archive:
        for name in names:
            member = archive.getmember(f"provider_core-{version}/{name}")
            if not member.isfile():
                raise ValueError("Cannot prepare consumer tests: shared support must consist of regular files.")
            destination = root / "run" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as stream, destination.open("xb") as target:
                shutil.copyfileobj(stream, target)


def release_source(release, pool, version):
    name = f"provider_core-{version}.tar.gz"
    with regular_file(release, os.O_RDONLY) as stream:
        record = json.load(stream)
    try:
        checksum = record["files"][name]
    except (KeyError, TypeError) as cause:
        raise ValueError(f"Shared release pin does not identify test support for installed provider-core {version}.") from cause
    return pool / name, checksum


def stage_support(release, pool, target):
    version = importlib.metadata.version("provider-core")
    source, checksum = release_source(release, pool, version)
    with tempfile.TemporaryDirectory(prefix="nmrnet-support-") as temporary:
        root = Path(temporary)
        copy_support(source, checksum, root, version)
        shutil.copytree(root / "run/tests", target / "tests")


def run_suite(release, pool, patterns):
    import provider_core

    prefix = Path(sys.prefix).resolve()
    origin = Path(provider_core.__file__).resolve()
    distribution = importlib.metadata.distribution("provider-core")
    if not origin.is_relative_to(prefix) or not Path(distribution.locate_file("provider_core")).resolve().is_relative_to(prefix):
        raise RuntimeError("Cannot run consumer tests: provider-core must come from the installed runtime environment.")
    source, source_sha256 = release_source(release, pool, distribution.version)
    with tempfile.TemporaryDirectory(prefix="nmrnet-consumer-") as temporary:
        root = Path(temporary)
        copy_support(source, source_sha256, root, distribution.version)
        run = root / "run"
        shutil.copytree("/consumer-tests", run / "tests/integration/nmrnet")
        with chdir(run):
            sys.path.insert(0, str(run))
            sys.path.append("/consumer")
            suite = unittest.TestSuite()
            for pattern in patterns:
                selected = unittest.defaultTestLoader.discover(
                    str(run / "tests/integration/nmrnet"), pattern=pattern, top_level_dir=str(run)
                )
                if selected.countTestCases() == 0:
                    raise RuntimeError(f"Selected NMRNet consumer pattern contains no tests: {pattern}")
                suite.addTests(selected)
            print(f"NMRNet consumer tests use provider-core {distribution.version} from {origin}; support SHA-256 {source_sha256}.", flush=True)
            return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "stage-support", "fixture"))
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--target", type=Path)
    entrypoint = parser.add_mutually_exclusive_group()
    entrypoint.add_argument("--script", type=Path)
    entrypoint.add_argument("--module", choices=("unittest",))
    parser.add_argument("--pattern", nargs="+", default=["test*.py"])
    args, remaining = parser.parse_known_args()
    if args.action in {"stage-support", "fixture"}:
        if args.target is None:
            parser.error("support staging requires --target for the isolated fixture directory")
        stage_support(args.release, args.pool, args.target)
        if args.action == "fixture":
            if args.script is None and args.module is None:
                parser.error("fixture requires --script or --module for its qualification entrypoint")
            sys.path.insert(0, str(args.target))
            sys.argv = [str(args.script or args.module), *(remaining[1:] if remaining[:1] == ["--"] else remaining)]
            if args.script is not None:
                runpy.run_path(str(args.script), run_name="__main__")
            else:
                runpy.run_module(args.module, run_name="__main__")
        elif remaining or args.script is not None or args.module is not None:
            parser.error("stage-support does not run a qualification entrypoint")
    else:
        if args.target is not None or args.script is not None or args.module is not None or remaining:
            parser.error("run accepts only its release, pool and consumer patterns")
        raise SystemExit(run_suite(args.release, args.pool, args.pattern))
