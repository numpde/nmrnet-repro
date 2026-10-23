"""Retain one control session separately from its scientific endpoint."""

from provider_core.container_guard import require_container

require_container()

import socket
import time
from dataclasses import dataclass

from provider_core.runner_protocol import LifecycleRejected, positive_seconds, receive_document, receive_greeting


@dataclass(frozen=True, slots=True)
class Retirement:
    returncode: int
    scratch_released: bool


class RunnerSession:
    """A synchronous I/O owner, retained by async callers through their original task.

    Construct before starting connect. request_stop half-closes control immediately;
    it does not wait for scientific exchange or claim that cleanup has completed.
    """

    def __init__(self, path, *, role, expected_lock_identity, startup_seconds, retirement_seconds):
        positive_seconds(startup_seconds)
        positive_seconds(retirement_seconds)
        self.path, self._startup, self._cleanup = str(path), startup_seconds, retirement_seconds
        self.role, self._expected_lock = role, tuple(expected_lock_identity)
        self.control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.science = None
        self.generation = self.operation = self.lock_identity = None
        self.retirement = None
        self._stop_requested = False
        self._observation_failure = None
        self._connect_started = False

    def connect(self):
        if self._connect_started:
            raise ValueError("Runner session connect is single-use; retain its original setup operation.")
        self._connect_started = True
        deadline = time.monotonic() + self._startup
        self.control.settimeout(self._startup)
        self.control.connect(self.path)
        greeting, self.science = receive_greeting(self.control, deadline=deadline)
        if greeting["role"] != self.role or tuple(greeting["lock"]) != self._expected_lock:
            self.science.close()
            self.science = None
            raise LifecycleRejected("Runner greeting does not match the admitted role and stable lifetime lock.")
        self.generation, self.operation = greeting["generation"], greeting["operation"]
        self.lock_identity = tuple(greeting["lock"])

    def request_stop(self):
        if not self._stop_requested:
            self.control.shutdown(socket.SHUT_WR)
            self._stop_requested = True

    def retire(self):
        if self.retirement is None:
            self.request_stop()
        self.observe_retirement()

    def observe_retirement(self):
        """Receive cleanup facts without causing retirement (including cap expiry)."""
        if self._observation_failure is not None:
            raise LifecycleRejected(
                "Original runner retirement read failed; preserve its cause and use bounded lifecycle recovery, not a second frame read."
            ) from self._observation_failure
        if self.retirement is None:
            self.control.settimeout(self._cleanup)
            try:
                self.retirement = self._receive_retirement()
            except Exception as cause:
                # A partial frame cannot restart at its remaining bytes. Retain
                # this owner until the binding's proof session settles it.
                self._observation_failure = cause
                raise
        if not self.retirement.scratch_released:
            raise LifecycleRejected("Runner child stopped but its scratch cleanup remains unconfirmed.",
                                    returncode=self.retirement.returncode, scratch_released=False)

    def _receive_retirement(self):
        reply = receive_document(self.control, deadline=time.monotonic() + self._cleanup)
        if (
            set(reply) != {"kind", "generation", "operation", "returncode", "scratch_released"}
            or reply["kind"] != "retired" or reply["generation"] != self.generation
            or reply["operation"] != self.operation or type(reply["returncode"]) is not int
            or type(reply["scratch_released"]) is not bool
        ):
            raise LifecycleRejected("Runner retirement is malformed or belongs to another operation.")
        return Retirement(reply["returncode"], reply["scratch_released"])

    def close(self):
        if self.science is not None:
            self.science.close()
        self.control.close()
