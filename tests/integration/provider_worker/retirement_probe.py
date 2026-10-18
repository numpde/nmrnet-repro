"""Contained endpoints for the real-engine retirement qualification.

The worker deliberately ignores TERM while blocked. The controller can request
retirement but cannot inspect Docker or manufacture the host's receipt. This is
an ownership witness, not a scientific or API integration test.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import errno
import signal
import sys
import threading
import time
from pathlib import Path

from nmrnet_provider.retirement import HostRetirement, RetirementUnconfirmed


def wait_for(path, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with path.open("rb") as source:
                return source.read(257)
        except FileNotFoundError:
            time.sleep(0.02)
    raise TimeoutError(f"Host lifecycle probe did not publish {path.name} within {seconds} seconds")


def worker(allocation):
    if Path("/control").exists() or Path("/var/run/docker.sock").exists():
        raise RuntimeError("Worker was granted controller or engine authority")
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path("/ipc/ready").write_text(allocation + "\n")
    threading.Event().wait()


def controller(allocation, scenario):
    if Path("/var/run/docker.sock").exists():
        raise RuntimeError("Controller was granted engine authority")
    if wait_for(Path("/ipc/ready"), 10) != (allocation + "\n").encode():
        raise RuntimeError("Ready worker belongs to another allocation")
    try:
        Path("/control/receipt/forged").write_bytes(b"retired\n")
    except OSError as cause:
        # A read-only mount is the intended denial, not arbitrary I/O failure.
        if cause.errno != errno.EROFS:
            raise
    else:
        raise RuntimeError("Controller could forge a host retirement receipt")
    retirement = HostRetirement(
        allocation=allocation,
        request_directory=Path("/control/request"),
        receipt_directory=Path("/control/receipt"),
        operation_seconds=45,
    )
    try:
        asyncio.run(retirement.retire())
    except RetirementUnconfirmed:
        if scenario != "grant-failure":
            raise
    else:
        if scenario != "success":
            raise RuntimeError("Incomplete grant cleanup was accepted as retirement")
    receipt = wait_for(Path("/control/receipt/result"), 45)
    expected = "retired" if scenario == "success" else "unconfirmed"
    if receipt != (expected + " " + allocation + "\n").encode():
        raise RuntimeError("Host retirement receipt has the wrong allocation or disposition")
    if scenario == "success" and Path("/ipc/ready").exists():
        raise RuntimeError("Host confirmed retirement before releasing worker IPC files")
    Path("/control/request/observed").write_text(expected + " " + allocation + "\n")


if __name__ == "__main__":
    mode, allocation, scenario = sys.argv[1:]
    if mode == "worker":
        worker(allocation)
    elif mode == "controller" and scenario in {"success", "grant-failure"}:
        controller(allocation, scenario)
    else:
        raise ValueError("Retirement probe requires an explicit endpoint and scenario")
