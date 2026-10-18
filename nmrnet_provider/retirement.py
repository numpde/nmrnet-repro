"""Observe host-owned retirement without granting the controller engine access.

The fixed request/receipt protocol is qualified by tools/worker_qualification.sh
at 1de1074. Composition grants a fresh private request directory and a separate
read-only host receipt directory; neither is mounted into the scientific worker.
Only that host owner may publish confirmation after exact-container/grant cleanup.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import os
import stat
import time

from nmrnet_provider.worker_protocol import require_allocation, require_seconds


class RetirementUnconfirmed(RuntimeError):
    """Safe caller text; chained filesystem evidence remains protected."""


class HostRetirement:
    """One allocation, one request attempt, and retained asynchronous observation.

    Paths are composition-owned mounts, never Job/interpreter inputs. This owner
    must outlive caller cancellation. A timeout cannot stop filesystem I/O: the
    original task remains held, and later calls await it before doing anything
    else. After a completed observation fails, another call may only read again.
    Failed publication or descriptor cleanup remains unresolved for recovery;
    later receipt observations cannot erase that local uncertainty.
    """

    def __init__(self, *, allocation, request_directory, receipt_directory, operation_seconds):
        require_allocation(allocation)
        require_seconds(operation_seconds)
        self._allocation = allocation
        self._request = request_directory / "retire"
        self._receipt = receipt_directory / "result"
        self._seconds = operation_seconds
        self._operation = None
        self._requested = False
        self._effect_failure = None
        self._confirmed = False
        self.last_failure = None

    async def retire(self):
        """Return only after exact host confirmation and completed local I/O."""
        if self._confirmed:
            return
        if self._effect_failure is not None:
            raise self._effect_failure
        if self._operation is not None and self._operation.cancelled():
            raise RetirementUnconfirmed("Retirement observation was interrupted internally; preserve the allocation until its I/O is reconciled.")
        if self._operation is not None and self._operation.done() and self._operation.exception() is None:
            self._confirmed = True
            return
        if self._operation is None or self._operation.done():
            publish = not self._requested
            self._requested = True
            self._operation = asyncio.create_task(asyncio.to_thread(self._run, publish))
            self._operation.add_done_callback(self._record_failure)
        try:
            async with asyncio.timeout(self._seconds):
                await asyncio.shield(self._operation)
            self._confirmed = True
        except TimeoutError as cause:
            raise RetirementUnconfirmed("Retirement confirmation exceeded its waiting budget; preserve the allocation and original observation.") from cause

    def _record_failure(self, operation):
        # Retrieve late exceptions even when the awaiting caller was cancelled;
        # event-loop diagnostics must not print private filesystem causes.
        if not operation.cancelled():
            failure = operation.exception()
            if failure is not None:
                self.last_failure = failure

    def _run(self, publish):
        if publish:
            try:
                self._publish_request()
            except Exception as cause:
                failure = RetirementUnconfirmed("Cannot confirm retirement request publication; preserve its private failure and allocation for recovery.")
                self._effect_failure = failure
                raise failure from cause
        deadline = time.monotonic() + self._seconds
        while time.monotonic() < deadline:
            receipt = self._read_receipt()
            if receipt is not None:
                if receipt == f"retired {self._allocation}\n".encode("ascii"):
                    return
                if receipt == f"unconfirmed {self._allocation}\n".encode("ascii"):
                    raise RetirementUnconfirmed("The host could not confirm worker retirement and grant cleanup; preserve the allocation for recovery.")
                raise RetirementUnconfirmed("Host retirement receipt is malformed or belongs to another allocation; no cleanup is confirmed.")
            time.sleep(0.02)
        raise RetirementUnconfirmed("The host did not publish retirement confirmation within the observation budget; preserve the allocation.")

    def _publish_request(self):
        # Exclusive creation refuses implicit adoption by a fresh adapter. The
        # host admits complete exact bytes before acting, even during this write.
        descriptor = os.open(self._request, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(f"retire {self._allocation}\n".encode("ascii"))

    def _read_receipt(self):
        try:
            descriptor = os.open(self._receipt, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        except OSError as cause:
            raise RetirementUnconfirmed("Cannot read the host retirement receipt; preserve the allocation and private filesystem cause.") from cause
        try:
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise RetirementUnconfirmed("Host retirement receipt is not a regular file; no cleanup is confirmed.")
                return os.read(descriptor, 257)
            finally:
                try:
                    os.close(descriptor)
                except OSError as cause:
                    failure = RetirementUnconfirmed("Cannot confirm retirement receipt descriptor cleanup; preserve the allocation and private filesystem cause.")
                    self._effect_failure = failure
                    raise failure from cause
        except OSError as cause:
            raise RetirementUnconfirmed("Cannot finish reading the host retirement receipt; preserve the allocation and private filesystem cause.") from cause
