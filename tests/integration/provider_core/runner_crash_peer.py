"""Observe one real reader's crash without sending scientific input.

pidfd_open(2), NOTES, defines whole-process exit readiness with flags=0:
https://man7.org/linux/man-pages/man2/pidfd_open.2.html#NOTES
SCM_RIGHTS retains that same open description after the probe dies:
https://man7.org/linux/man-pages/man7/unix.7.html
The observer never infers exit from a broken stream or invents an exit code.
"""

from provider_core.container_guard import require_container

require_container()

import array
import fcntl
import os
import select
import socket
import time
from contextlib import ExitStack

from provider_core.response_json import read_document
from provider_core.runner_session import RunnerSession


def reader_crash_witness(root, leaf, publish, publish_bytes):
    deadline = time.monotonic() + 60
    probe_path = leaf / "crash-probe.sock"
    bound_identity = None

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Reader crash witness exceeded its original 60-second allowance.")
        return seconds

    try:
        with ExitStack() as owned:
            lock = owned.enter_context((leaf / ".owner-session.lock").open("rb"))
            observed = os.fstat(lock.fileno())
            identity = observed.st_dev, observed.st_ino
            old = RunnerSession(leaf / "runner.sock", role="reader", expected_lock_identity=identity,
                                startup_seconds=remaining(), retirement_seconds=remaining())
            owned.callback(old.close)
            old.connect(deadline=deadline)
            listener = owned.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET))
            listener.bind(str(probe_path))
            os.chmod(probe_path, 0o600)
            observed = probe_path.lstat()
            bound_identity = observed.st_dev, observed.st_ino
            listener.listen(1)
            listener.settimeout(remaining())
            publish_bytes("reader-crash-operation", (old.operation + "\n").encode())
            connection, _ = listener.accept()
            owned.enter_context(connection)
            connection.settimeout(remaining())
            payload, ancillary, flags, _ = connection.recvmsg(
                4096, socket.CMSG_SPACE(8 * array.array("i").itemsize), socket.MSG_CMSG_CLOEXEC)
            descriptors = []
            for level, kind, content in ancillary:
                if (level, kind) == (socket.SOL_SOCKET, socket.SCM_RIGHTS):
                    values = array.array("i")
                    values.frombytes(content[:len(content) - len(content) % values.itemsize])
                    for descriptor in values:
                        owned.callback(os.close, descriptor)
                        descriptors.append(descriptor)
            assert not flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC), "Reader crash probe was truncated."
            assert (len(ancillary) == 1
                    and ancillary[0][:2] == (socket.SOL_SOCKET, socket.SCM_RIGHTS)
                    and len(descriptors) == 1), "Reader crash probe must transfer exactly one child pidfd."
            child_fd, = descriptors
            assert (os.readlink(f"/proc/self/fd/{child_fd}") == "anon_inode:[pidfd]"), "Reader probe did not transfer a process handle."
            facts = read_document(payload, 4096)
            assert type(facts) is dict and set(facts) == {
                "operation", "lease", "supervisor_pid", "supervisor_start_ticks", "child_pid", "child_start_ticks",
                "stopped", "alive_after_term", "remnant_written"}, "Reader crash probe returned unexpected facts."
            assert (facts["operation"] == old.operation and facts["lease"] == list(identity)), "Probe changed the operation or stable lease identity."
            assert all(type(facts[key]) is int and facts[key] > 1
                       for key in ("supervisor_pid", "child_pid")), "Probe did not identify native process IDs."
            assert all(type(facts[key]) is int and facts[key] > 0
                       for key in ("supervisor_start_ticks", "child_start_ticks")), "Probe omitted process start identities."
            assert facts["supervisor_pid"] != facts["child_pid"], "Probe confused child and supervisor."
            assert all(facts[key] is True for key in ("stopped", "alive_after_term", "remnant_written")), \
                "Probe did not confirm the blocked child and scratch remnant."
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError("Stopped reader did not retain its lifetime lease.")
            poller = select.poll()
            poller.register(child_fd, select.POLLIN | select.POLLHUP)
            assert not poller.poll(0), "Old reader exited before the supervisor crash."
            connection.sendall(b"GO " + old.operation.encode())
            events = poller.poll(max(1, int(remaining() * 1000)))
            assert (len(events) == 1 and events[0][1] & (select.POLLIN | select.POLLHUP)
                    and not events[0][1] & ~(select.POLLIN | select.POLLHUP)), "The retained pidfd did not confirm old-reader exit."
            old.close()
            publish("reader-old-child-exited.json", facts)
            marker = root / "reader-fresh-root"
            while not marker.exists():
                time.sleep(min(0.05, remaining()))
            assert marker.read_bytes() == b"fresh-root\n", "Host did not publish its completed fresh-scratch observation."
            observed = (leaf / ".owner-session.lock").stat()
            assert (observed.st_dev, observed.st_ino) == identity, "Reader restart replaced its stable lease inode."
            successor = RunnerSession(leaf / "runner.sock", role="reader", expected_lock_identity=identity,
                                      startup_seconds=remaining(), retirement_seconds=remaining())
            owned.callback(successor.close)
            successor.connect(deadline=deadline)
            assert (successor.generation != old.generation and successor.operation != old.operation), \
                "Reader reused the old generation or operation after restart."
            successor.retire(deadline=deadline)
            assert successor.retirement.scratch_released, "Successor scratch cleanup remains unconfirmed."
            publish("reader-crash-receipt.json", {
                "old_operation": old.operation, "new_operation": successor.operation,
                "old_generation": old.generation, "new_generation": successor.generation,
                "lease": list(identity), "old_child_exit_confirmed_by_pidfd": True,
                "old_child_exit_code": None, "successor_returncode": successor.retirement.returncode,
                "successor_scratch_released": True, "scientific_input_bytes_sent": 0})
    finally:
        if bound_identity is not None:
            try:
                observed = probe_path.lstat()
            except FileNotFoundError:
                pass
            else:
                if (observed.st_dev, observed.st_ino) == bound_identity:
                    probe_path.unlink()
