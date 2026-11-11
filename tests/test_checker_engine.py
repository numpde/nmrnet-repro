#!/usr/bin/env python3
"""Finite native Docker fixture for the checker launcher; never contacts an engine."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from tools import checker


def main():
    arguments = sys.argv[1:]
    state = Path(os.environ["CHECKER_FIXTURE_STATE"])
    configuration = None
    if arguments[:1] == ["--config"]:
        configuration = Path(arguments[1])
        arguments = arguments[2:]
    if arguments[:2] != ["--host", "unix:///var/run/docker.sock"]:
        raise SystemExit(97)
    arguments = arguments[2:]
    # Admission inherits the 4096-byte receipt limit. Keep each observation in
    # its own bounded file, publishing only complete JSON to failure rescue.
    # Engine steps are sequential in this finite caller fixture.
    calls = state / "calls"
    # The launcher fixture owns this directory, as in the shared container
    # fixture's record_engine_call; a missing ledger must fail before effects.
    observations = [path for path in calls.iterdir() if path.name.endswith(".json")]
    observation = calls / f"{len(observations):06d}.json"
    pending = observation.with_suffix(".pending")
    with pending.open("w") as stream:
        json.dump(
            {
                "arguments": arguments,
                "pid": os.getpid(),
                "process_group": os.getpgrp(),
                # silent-defaults: allow-contract -- Inspection calls record their stdout receipt directory; other calls record None for rescue to skip; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_actual_inspection_signals_exit_before_draining_full_stderr
                "receipt_directory": str(Path("/proc/self/fd/1").readlink().parent)
                if arguments[:2] == ["image", "inspect"]
                else None,
                "ambient_engine": any(
                    name in os.environ
                    for name in (
                        "DOCKER_HOST",
                        "DOCKER_CONTEXT",
                        "DOCKER_TLS_VERIFY",
                        "DOCKER_CERT_PATH",
                        "BUILDX_BUILDER",
                        "BUILDX_CONFIG",
                    )
                ),
                "buildkit": os.environ.get("DOCKER_BUILDKIT"),
                # silent-defaults: allow-contract -- None records an absent --config option; the consumer checks the supplied private path and its retirement; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_private_client_scrubs_poison_and_check_matches_printed_worker
                "client_config": str(configuration) if configuration is not None else None,
                # silent-defaults: allow-contract -- None records an absent --config option; the consumer requires mode 0700 for the observed private directory; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_private_client_scrubs_poison_and_check_matches_printed_worker
                "config_mode": configuration.stat().st_mode & 0o777 if configuration is not None else None,
                # silent-defaults: allow-contract -- None records an absent --config option; the consumer requires an empty entry list for the observed private directory; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_private_client_scrubs_poison_and_check_matches_printed_worker
                "config_entries": sorted(path.name for path in configuration.iterdir()) if configuration is not None else None,
                "environment": {
                    name: value
                    for name, value in os.environ.items()
                    if name.startswith(("DOCKER_", "BUILDX_")) or name.lower().endswith("_proxy")
                },
            },
            stream,
        )
        stream.write("\n")
    pending.replace(observation)
    # silent-defaults: allow-contract -- The two-token image inspect command uses the inspect hold phase; other commands use their first token; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_actual_inspection_signals_exit_before_draining_full_stderr
    phase = "inspect" if arguments[:2] == ["image", "inspect"] else arguments[0]
    if os.environ.get("CHECKER_FIXTURE_HOLD") == phase and (
        "CHECKER_FIXTURE_HOLD_CALL" not in os.environ or os.environ["CHECKER_FIXTURE_HOLD_CALL"] == str(len(observations) + 1)
    ):
        for interrupt in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(interrupt, signal.SIG_IGN)
        (state / "client-ready").write_text(str(os.getpid()))
        while True:
            time.sleep(1)
    if arguments[0] == "run":
        mounts = {}
        for index, argument in enumerate(arguments):
            if argument == "--mount":
                fields = dict(item.split("=", 1) for item in arguments[index + 1].split(",") if "=" in item)
                mounts[fields["dst"]] = Path(fields["src"])
        if arguments[arguments.index("--network") + 1] != "none" or arguments[arguments.index("-m") + 1] != "tools.checker":
            raise SystemExit(97)
        document = checker.declaration(mounts["/input/dependencies.json"])
        if document["base"] != arguments[arguments.index("--base") + 1]:
            raise SystemExit(97)
        checker.check(mounts["/input/dependencies.json"], mounts["/dependencies"], document["base"], mounts["/wheelhouse"])
        sys.stdout.write(hashlib.sha256(mounts["/input/dependencies.json"].read_bytes()).hexdigest() + "\n")
        if "CHECKER_FIXTURE_RUN_STATUS" in os.environ and (
            "CHECKER_FIXTURE_RUN_STATUS_CALL" not in os.environ
            or os.environ["CHECKER_FIXTURE_RUN_STATUS_CALL"] == str(len(observations) + 1)
        ):
            sys.stderr.write("checker run diagnostic\n")
            raise SystemExit(int(os.environ["CHECKER_FIXTURE_RUN_STATUS"]))
    elif arguments[0] == "build":
        if os.environ.get("CHECKER_FIXTURE_PROBE_LEASE") == "1":
            if subprocess.run(["flock", "-n", "-x", str(state.parent), "true"], check=False).returncode == 0:
                raise SystemExit(97)
            (state / "build-lease-held").touch()
        fail_build = os.environ.get("CHECKER_FIXTURE_BUILD_FAIL") == "1"
        if not fail_build:
            build_output = os.environ["CHECKER_FIXTURE_BUILD_OUTPUT"]
        if arguments[-1] != "-" or "--build-context" in arguments:
            raise SystemExit(97)
        capture = next((state.parent / ".work/checker").glob(".build.*"))
        temporary = tempfile.TemporaryDirectory(prefix="checker-stream-fixture-")
        context = Path(temporary.name)
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
            archive.extractall(context, filter="data")
        dependencies = context / "checker-dependencies"
        declaration = context / "requirements/checker-dependencies.json"
        checker.check(declaration, dependencies, checker.declaration(declaration)["base"], context / "checker-wheels")
        mutation = os.environ.get("CHECKER_FIXTURE_MUTATE")
        if mutation:
            Path(mutation).write_bytes(b"later live source mutation\n")
        manifest = {
            str(path.relative_to(context)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in context.rglob("*")
            if path.is_file()
        }
        (state / "build.json").write_text(
            json.dumps({"context": str(capture / "context"), "manifest": manifest, "dependencies": str(capture / "dependencies")})
        )
        label = arguments[arguments.index("--label") + 1].split("=", 1)[1]
        (state / "label").write_text(label)
        if fail_build:
            raise SystemExit(7)
        sys.stdout.write(build_output)
    elif arguments[:2] == ["image", "inspect"]:
        if arguments != [
            "image",
            "inspect",
            "--format",
            '{{.Id}} {{index .Config.Labels "io.nmrnet-repro.checker-inputs"}}',
            "sha256:" + "d" * 64,
        ]:
            raise SystemExit(97)
        if arguments[-1] != "sha256:" + "d" * 64:
            raise SystemExit(97)
        identifier = os.environ["CHECKER_FIXTURE_INSPECTED_ID"]
        receipt = identifier + " " + (state / "label").read_text() + "\n"
        fault = os.environ.get("CHECKER_FIXTURE_RECEIPT")
        if fault == "missing-lf":
            receipt = receipt[:-1]
        elif fault == "extra-lf":
            receipt += "\n"
        elif fault == "wrong-label":
            receipt = identifier + " " + "f" * 64 + "\n"
        elif fault == "large-stdout":
            receipt = "x" * 8192
        elif fault == "cap-stdout":
            receipt = "x" * 4096
        sys.stdout.write(receipt)
        if fault == "large-stderr":
            sys.stderr.write("x" * 8192)
        elif fault == "cap-stderr":
            sys.stderr.write("x" * 4096)
        if fault in {"link", "fifo"}:
            destination = Path("/proc/self/fd/1").readlink()
            destination.unlink()
            if fault == "link":
                destination.symlink_to(state / "unread-sentinel")
            else:
                os.mkfifo(destination)
        if "CHECKER_FIXTURE_INSPECT_STATUS" in os.environ:
            sys.stderr.write("checker daemon cause\n")
            raise SystemExit(int(os.environ["CHECKER_FIXTURE_INSPECT_STATUS"]))
    else:
        raise SystemExit(97)


if __name__ == "__main__":
    main()
