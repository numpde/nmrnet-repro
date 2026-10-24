"""Stop a real reader child, then crash only its exact supervisor.

Run through bounded Docker exec in the admitted reader, never on the host.
Numeric PIDs discover handles; pidfd_send_signal(2) targets the original tasks:
https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html#DESCRIPTION
The external peer receives the child pidfd before namespace teardown kills us.
"""

from provider_core.container_guard import require_container

require_container()

import array
import json
import os
import re
import select
import signal
import socket
import sys
import time
from pathlib import Path

from provider_core.process_status import read_status


def main(operation, seconds):
    assert re.fullmatch(r"[0-9a-f]{32}", operation)
    assert type(seconds) is int and seconds > 0
    deadline = time.monotonic() + seconds

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError(f"Reader crash probe exceeded its original {seconds}-second allowance.")
        return seconds

    def identity(pid):
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return fields[0], int(fields[1]), int(fields[19])

    def live(descriptor):
        poller = select.poll()
        poller.register(descriptor, select.POLLIN)
        assert not poller.poll(0), "Crash probe target exited before supervisor kill."

    status = read_status("/run/status.json", "reader")
    assert (status["phase"] == "working"
            and 0 <= time.monotonic() - status["updated_monotonic"] <= 30), "Reader has no current working supervisor."
    supervisor = status["pid"]
    supervisor_fd = os.pidfd_open(supervisor, 0)
    live(supervisor_fd)
    assert identity(supervisor)[2] == status["start_ticks"]
    work = f"/scratch/{operation}"
    candidates = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        try:
            observed = identity(pid)
            if observed[1] == supervisor and os.readlink(entry / "cwd") == work:
                candidates.append((pid, observed[2]))
        except (FileNotFoundError, ProcessLookupError):
            continue
    assert len(candidates) == 1, "Crash probe cannot identify the reader's unique direct child."
    child, child_ticks = candidates[0]
    child_fd = os.pidfd_open(child, 0)
    live(child_fd)
    assert identity(child)[1:] == (supervisor, child_ticks)
    assert os.readlink(f"/proc/{child}/cwd") == work
    observed = Path("/native/reader/.owner-session.lock").stat()
    lease = observed.st_dev, observed.st_ino
    inherited = []
    for entry in Path(f"/proc/{child}/fd").iterdir():
        try:
            metadata = entry.stat()
            if (metadata.st_dev, metadata.st_ino) == lease:
                inherited.append(int(entry.name))
        except FileNotFoundError:
            continue
    assert inherited, "Reader child did not inherit the stable lease."
    descriptor = os.open(f"{work}/crash-probe-remnant", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(b"old operation\n")
        output.flush()
        os.fsync(output.fileno())
    signal.pidfd_send_signal(child_fd, signal.SIGSTOP)
    while identity(child)[0] != "T":
        live(child_fd)
        time.sleep(min(0.01, remaining()))
    inherited_lease = Path(f"/proc/{child}/fd/{inherited[0]}").stat()
    assert (inherited_lease.st_dev, inherited_lease.st_ino) == lease, "Stopped child lost its inherited lease."
    signal.pidfd_send_signal(child_fd, signal.SIGTERM)
    poller = select.poll()
    poller.register(child_fd, select.POLLIN)
    assert not poller.poll(max(1, int(min(0.1, remaining()) * 1000)))
    assert identity(child) == ("T", supervisor, child_ticks)
    facts = json.dumps({"operation": operation, "lease": list(lease),
        "supervisor_pid": supervisor, "supervisor_start_ticks": status["start_ticks"],
        "child_pid": child, "child_start_ticks": child_ticks,
        "stopped": True, "alive_after_term": True, "remnant_written": True}, separators=(",", ":")).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as control:
        control.settimeout(remaining())
        control.connect("/native/reader/crash-probe.sock")
        assert control.sendmsg([facts], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [child_fd]))]) == len(facts)
        control.settimeout(remaining())
        assert control.recv(128) == b"GO " + operation.encode()
    live(child_fd)
    live(supervisor_fd)
    assert identity(supervisor)[2] == status["start_ticks"]
    assert identity(child) == ("T", supervisor, child_ticks)
    remaining()
    signal.pidfd_send_signal(supervisor_fd, signal.SIGKILL)
    # Docker init/container teardown should kill this exec probe as well. Its
    # stream exit is not proof of child exit; the surviving peer owns that proof.
    while True:
        time.sleep(min(0.05, remaining()))


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
