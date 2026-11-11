#!/usr/bin/env python3
"""Finite native-client observations and command faults for ownership tests."""

from tools.container_guard import require_container

require_container()

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def fixture_command(name):
    """Inject only named local file faults; all other operations use native tools."""
    arguments = sys.argv[1:]
    if name == "mv":
        phase = os.environ["NATIVE_PUBLICATION_FAILURE"]
        source, target = map(Path, arguments[-2:])
        receipt = target.parent / "status.ready"
        if target.name == "status.ready":
            anchor = int(source.read_text())
        elif receipt.exists():
            anchor = int(receipt.read_text())
        else:
            anchor = 0
        Path(os.environ["NATIVE_CONTROL"]).write_text(json.dumps({"anchor": anchor, "directory": str(target.parent)}))
        if target.name == "status.ready" and os.environ.get("NATIVE_CORRUPT_READY") == "1":
            source.write_bytes(b"not-the-anchor\n")
        if target.name == "status.release" and phase.startswith("release-"):
            if phase == "release-fifo":
                os.mkfifo(target)
            elif phase == "release-directory":
                target.mkdir()
            else:
                target.symlink_to(source)
            return 0
        if target.name == "status." + phase or (target.name == "status" and phase == "status"):
            if os.environ.get("NATIVE_CANCEL_PUBLICATION") == "1":
                parent = int(Path("/proc", str(anchor), "stat").read_text().split()[3])
                os.kill(parent, signal.SIGTERM)
            return 29
        if target.name == "status":
            mutation = os.environ["NATIVE_STATUS_MUTATION"]
            values = {
                "nul": b"0\0\n",
                "cr": b"0\r\n",
                "missing-lf": b"0",
                "extra-lf": b"0\n\n",
                "extra-line": b"0\n7\n",
                "negative": b"-1\n",
                "256": b"256\n",
                "text": b"unknown\n",
            }
            if mutation in values:
                source.write_bytes(values[mutation])
            if mutation == "symlink":
                target.symlink_to(source)
                return 0
            if mutation in {"fifo", "directory", "deadline-fifo", "deadline-directory", "deadline-symlink"}:
                if mutation.startswith("deadline-"):
                    target = target.parent / "status.deadline"
                if mutation.endswith("fifo"):
                    os.mkfifo(target)
                elif mutation.endswith("directory"):
                    target.mkdir()
                else:
                    target.symlink_to(source)
                return 0
    if name == "head" and arguments[-1].endswith("/status") and os.environ["NATIVE_STATUS_MUTATION"] == "read-error":
        return 31
    if name == "head" and arguments == ["-c", "8388608"] and os.environ.get("NATIVE_DRAIN_FAILURE") == "1":
        return 33
    if name == "rm" and os.environ.get("NATIVE_CLEANUP_FAILURE") == "1":
        if "NATIVE_CANCEL_CLEANUP" in os.environ:
            os.kill(os.getppid(), int(os.environ["NATIVE_CANCEL_CLEANUP"]))
        return 23
    if name == "rm" and os.environ.get("NATIVE_CLEANUP_FAILURE") == "after-delete":
        # Actual deletion plus a secondary failure must not invent retained files
        # or block the stdio owner by writing another message to its native pipe.
        subprocess.run(["/bin/rm", *arguments], check=True)
        if "NATIVE_CANCEL_CLEANUP" in os.environ:
            os.kill(os.getppid(), int(os.environ["NATIVE_CANCEL_CLEANUP"]))
        sys.stderr.write("fixture cleanup failed after deletion\n")
        return 23
    os.execv("/bin/" + name, [name, *arguments])
    return 97


def native_client():
    """Model a finite native status, resistant group member or output-only EOF."""
    observation = Path(os.environ["NATIVE_OBSERVATION"])
    behavior = os.environ["NATIVE_BEHAVIOR"]
    observed = {
        "pid": os.getpid(),
        "parent": os.getppid(),
        "sigint_ignored": signal.getsignal(signal.SIGINT) == signal.SIG_IGN,
        "sigterm_ignored": signal.getsignal(signal.SIGTERM) == signal.SIG_IGN,
        "sighup_ignored": signal.getsignal(signal.SIGHUP) == signal.SIG_IGN,
        "stdin_eof": os.read(0, 1) == b"",
    }
    if behavior in {"resistant", "held-descendant", "stream-live", "blocked-output", "original-signals"}:
        descendant = subprocess.Popen(["/bin/bash", "-c", "trap '' HUP INT TERM; exec sleep 60"])
        observed["descendant"] = descendant.pid
    observation.with_suffix(".pending").write_text(json.dumps(observed))
    observation.with_suffix(".pending").replace(observation)
    if behavior in {"stream-live", "stream-finite"}:
        os.write(1, b"stdout\0\r\n")
        os.write(2, b"stderr\0\r\n")
    if behavior == "stream-live":
        time.sleep(60)
    if behavior == "stream-large":
        data = bytes(range(256)) * 32769
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(data)
        sys.stderr.buffer.flush()
    if behavior == "blocked-output":
        stream = os.environ["NATIVE_BLOCK_STREAM"]
        if stream == "stdout":
            sys.stdout.buffer.write(b"x" * 8388609)
            sys.stdout.buffer.flush()
        elif stream == "stderr":
            sys.stderr.buffer.write(b"x" * 8388609)
            sys.stderr.buffer.flush()
        else:
            raise ValueError("Blocked-output fixture requires explicit stdout or stderr")
        (observation.parent / "write-complete").write_text("finished")
    if behavior == "original-signals":

        def record_signal(signum, _frame):
            with (observation.parent / "signal-observed").open("a") as marker:
                marker.write(signal.Signals(signum).name.removeprefix("SIG") + "\n")

        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(signum, record_signal)
        ignored = sum(1 << (signum - 1) for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM))
        descendant_status = Path("/proc", str(descendant.pid), "status")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            fields = dict(line.split(":", 1) for line in descendant_status.read_text().splitlines() if ":" in line)
            if int(fields["SigIgn"].strip(), 16) & ignored == ignored:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("The actual resistant descendant did not ignore HUP, INT and TERM before readiness.")
        (observation.parent / "container-handlers-ready").write_text("ready\n")
        time.sleep(60)
    if behavior == "resistant":
        signal.signal(signal.SIGTERM, lambda *_: (observation.parent / "term-observed").write_text("TERM\n"))
        time.sleep(60)
    if behavior == "late-success":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(0.3)
    if behavior == "slow-success":
        time.sleep(0.4)
    if behavior == "closed-output":
        os.close(1)
        os.close(2)
        (observation.parent / "outputs-closed").write_text("closed")
        time.sleep(0.25)
    return int(os.environ["NATIVE_STATUS"])


if __name__ == "__main__":
    command_name = Path(sys.argv[0]).name
    if command_name in {"mv", "head", "rm"}:
        raise SystemExit(fixture_command(command_name))
    if sys.argv[1:] == ["native-client"]:
        raise SystemExit(native_client())
    raise SystemExit(97)
