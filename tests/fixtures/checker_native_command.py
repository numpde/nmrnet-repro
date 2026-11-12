#!/usr/bin/env python3
"""Native-command observations and faults for checker launcher tests."""

from tools.container_guard import require_container

require_container()

import json
import os
import subprocess
import sys
import time
from pathlib import Path


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
        if os.environ.get("CHECKER_FIXTURE_CHECK_RETIREMENT") == "1" and (
            target.name.startswith("nmrnet-docker-client.")
            or (target.parent == state.parent / ".work/checker" and target.name.startswith(".build."))
        ):
            client = int((state / "client-ready").read_text())
            stopped = not Path("/proc", str(client)).exists()
            with (state / "retirement-order.jsonl").open("a") as stream:
                stream.write(json.dumps({"target": str(target), "client_stopped": stopped}) + "\n")
            if not stopped:
                raise SystemExit("Checker cleanup started before the original native client stopped")
        # Preserve the original record/capture effect trace; shared native and
        # client removals have a separate trace rather than impersonating it.
        # silent-defaults: allow-contract -- Checker record and capture removals use the asserted cleanup ledger; shared native removals use a separate ledger; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_failed_capture_cleanup_preserves_build_status_and_reports_retained_capture
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
            # silent-defaults: allow-contract -- This fault-scenario counter starts at the first intercepted ready publication attempt; the inspection case requires exactly four publication attempts and the fourth-attempt fault before any next owner; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_unknown_inspection_startup_retains_all_paths_without_next_owner
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
    raise SystemExit(97)


if __name__ == "__main__":
    main()
