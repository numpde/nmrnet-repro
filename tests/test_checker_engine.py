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
import time
from pathlib import Path

from tools import checker


def main():
    arguments = sys.argv[1:]
    state = Path(os.environ["CHECKER_FIXTURE_STATE"])
    command = Path(sys.argv[0]).name
    if command == "bash":
        if len(arguments) >= 2 and arguments[0] in {"-c", "-eu"}:
            program = arguments[1] if arguments[0] == "-c" else arguments[2]
            if 'printf "Checker image recorded: %s\\n" "$1"' in program:
                (state / "report.json").write_text(json.dumps({"pid": os.getpid(), "anchor": os.getpgrp()}))
            if "Checker image inspection receipts retained at" in program:
                (state / "cleanup-report.json").write_text(json.dumps({"pid": os.getpid(), "anchor": os.getpgrp()}))
                if "CHECKER_FIXTURE_REPORT_STATUS" in os.environ:
                    source = (state.parent / "tools/checker.sh").read_text()
                    start = "if inference_native 'checker cleanup reporting' untimed stdio bash -eu -c '"
                    end = '\' bash "$inference_diagnostic"'
                    if source.count(start) != 1 or source.count(end) != 1:
                        raise SystemExit(97)
                    expected = source.split(start, 1)[1].split(end, 1)[0]
                    if arguments[:2] == ["-eu", "-c"] and program == expected:
                        raise SystemExit(int(os.environ["CHECKER_FIXTURE_REPORT_STATUS"]))
        os.execv("/bin/bash", ["bash", *arguments])
    if command == "rm":
        target = Path(arguments[-1])
        # Preserve the original record/capture effect trace; shared native and
        # client removals have a separate trace rather than impersonating it.
        trace = "cleanup.jsonl" if target.parent == state.parent / ".work/checker" else "native-cleanup.jsonl"
        with (state / trace).open("a") as stream:
            stream.write(json.dumps(arguments) + "\n")
        if target.name.startswith("nmrnet-docker-client.") and os.environ.get("CHECKER_FIXTURE_HOLD_REMOVAL"):
            (state / "removal-ready").write_text(str(target))
            while not (state / "removal-release").exists():
                time.sleep(0.01)
        for prefix, setting, status in (
            ("nmrnet-docker-client.", "CHECKER_FIXTURE_CLIENT_CLEANUP_FAIL", 41),
            ("nmrnet-checker-admission.", "CHECKER_FIXTURE_ADMISSION_CLEANUP_FAIL", 42),
        ):
            if target.name.startswith(prefix) and os.environ.get(setting):
                if os.environ[setting] == "delete":
                    subprocess.run(["/bin/rm", *arguments], check=True)
                raise SystemExit(status)
        if (
            os.environ.get("CHECKER_FIXTURE_CAPTURE_CLEANUP_FAIL") == "1"
            and arguments[:2] == ["-rf", "--"]
            and len(arguments) == 3
            and target.parent == state.parent / ".work/checker"
            and target.name.startswith(".build.")
        ):
            raise SystemExit(13)
        if (
            os.environ.get("CHECKER_FIXTURE_RECORD_CLEANUP_FAIL") == "1"
            and arguments[:2] == ["-f", "--"]
            and len(arguments) == 3
            and target.parent == state.parent / ".work/checker"
            and target.name.startswith(".image-id.")
        ):
            raise SystemExit(14)
        os.execv("/bin/rm", ["/bin/rm", *arguments])
    if command == "mv":
        if Path(arguments[-1]).name == "status.ready" and os.environ.get("CHECKER_FIXTURE_READY_FAIL"):
            count_path = state / "ready-count"
            count = int(count_path.read_text()) + 1 if count_path.exists() else 1
            count_path.write_text(str(count))
            if os.environ["CHECKER_FIXTURE_READY_FAIL"] == "1" or count == 4:
                with (state / "ready-fault").open("a") as stream:
                    stream.write("ready-failure\n")
                raise SystemExit(47)
        # Shared control receipts are not checker record publication attempts.
        if Path(arguments[-1]) != state.parent / ".work/checker/image-id":
            os.execv("/bin/mv", ["/bin/mv", *arguments])
        (state / "record-rename.json").write_text(json.dumps(arguments))
        if os.environ.get("CHECKER_FIXTURE_RECORD_RENAME_FAIL") == "1":
            source, destination = map(Path, arguments[-2:])
            if (
                arguments[:2] != ["-T", "--"]
                or len(arguments) != 4
                or source.parent != state.parent / ".work/checker"
                or not source.name.startswith(".image-id.")
                or destination != source.parent / "image-id"
            ):
                raise SystemExit(97)
            raise SystemExit(9)
        os.execv("/bin/mv", ["/bin/mv", *arguments])
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
                "client_config": str(configuration) if configuration is not None else None,
                "config_mode": configuration.stat().st_mode & 0o777 if configuration is not None else None,
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
    phase = "inspect" if arguments[:2] == ["image", "inspect"] else arguments[0]
    if os.environ.get("CHECKER_FIXTURE_HOLD") == phase:
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
        checker.check(mounts["/input/dependencies.json"], mounts["/dependencies"])
        sys.stdout.write(hashlib.sha256(mounts["/input/dependencies.json"].read_bytes()).hexdigest() + "\n")
        if "CHECKER_FIXTURE_RUN_STATUS" in os.environ:
            sys.stderr.write("checker run diagnostic\n")
            raise SystemExit(int(os.environ["CHECKER_FIXTURE_RUN_STATUS"]))
    elif arguments[0] == "build":
        fail_build = os.environ.get("CHECKER_FIXTURE_BUILD_FAIL") == "1"
        if not fail_build:
            build_output = os.environ["CHECKER_FIXTURE_BUILD_OUTPUT"]
        context = Path(arguments[-1])
        dependencies = Path(arguments[arguments.index("--build-context") + 1].split("=", 1)[1])
        checker.check(context / "requirements/checker-dependencies.json", dependencies)
        mutation = os.environ.get("CHECKER_FIXTURE_MUTATE")
        if mutation:
            Path(mutation).write_bytes(b"later live source mutation\n")
        manifest = {
            str(path.relative_to(context)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in context.rglob("*")
            if path.is_file()
        }
        (state / "build.json").write_text(
            json.dumps({"context": str(context), "manifest": manifest, "dependencies": str(dependencies)})
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
