"""Own serialized filesystem I/O without blocking the async control loop.

A cancelled/expired wait does not cancel a write. The original worker future and
operation remain owned until settle observes them. No other operation, including
close, can pass that gate. This is the explicit storage executor allowed by note
017, not an adapter for scientific callbacks or a queue of future work.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from provider_core.attempt_files import AttemptFiles, StoreFailed, StoreLimits, StoreRefused
from provider_core.attempt_record import RecoveryScope


@dataclass(frozen=True, slots=True)
class SettledStorage:
    """The result of the original operation, not a second storage call."""

    operation: str
    value: object = field(repr=False)


@dataclass(slots=True)
class _Pending:
    operation: str
    worker: object
    waiter: asyncio.Future
    closing: bool


def _observe_wrapper_failure(waiter):
    # The concurrent future remains the effect authority. Retrieve its asyncio
    # mirror's exception so abandoned waits cannot log private storage evidence.
    if not waiter.cancelled():
        waiter.exception()


class AttemptStore:
    """One directory owner shared by cooperating lanes on one event loop.

    Composition creates the private directory and supplies its recovery scope and
    full-obligation capacity policy. A successful admit confirms file and directory
    fsync; it is not an API acknowledgement or permission to execute science.
    """

    def __init__(self, root, *, scope, limits, operation_seconds):
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("Cannot configure Attempt store: root must be an absolute Path")
        if type(scope) is not RecoveryScope or type(limits) is not StoreLimits:
            raise ValueError("Cannot configure Attempt store: explicit recovery scope and storage limits are required")
        if type(operation_seconds) not in (int, float) or not math.isfinite(operation_seconds) or operation_seconds <= 0:
            raise ValueError("Cannot configure Attempt store: operation_seconds must be finite and positive")
        self._files = AttemptFiles(root, scope, limits)
        self._seconds = operation_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="provider-store")
        self._pending = None
        self._observing = False
        self._loop = None
        self._closed = False
        self._close_failed = False
        self.last_failure = None

    @property
    def pending_operation(self):
        return None if self._pending is None else self._pending.operation

    @property
    def retirement_confirmed(self):
        return self._closed and not self._close_failed and self._pending is None

    async def open(self):
        await self._run("open Attempt storage", self._files.open)

    async def records(self):
        return await self._run("read Attempt inventory", self._files.records)

    async def admit(self, record):
        return await self._run("retain pending start", self._files.admit, record)

    async def replace(self, expected, replacement):
        """Durably advance the exact retained record; no implicit retry or regression."""
        return await self._run("replace Attempt record", self._files.replace, expected, replacement)

    async def settle(self):
        """Observe the original I/O result; poison from failed durability remains."""
        if self._pending is None:
            raise StoreRefused("No unresolved storage operation is available to settle")
        operation = self._pending.operation
        value = await self._observe()
        return SettledStorage(operation, value)

    async def close(self):
        if self._closed:
            if self._close_failed:
                raise StoreRefused("Attempt storage retirement failed; enclosing process retirement is required")
            return
        if self._pending is not None:
            raise StoreRefused("Cannot close Attempt storage while an original I/O result remains unobserved; settle it first")
        await self._run("close Attempt storage", self._files.close, closing=True)

    async def _run(self, operation, function, *args, closing=False):
        if self._closed or self._pending is not None:
            raise StoreRefused("Attempt store is closed or owns an unobserved I/O result; settle pending work before reuse")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise StoreRefused("Attempt store operations must remain on their owning event loop")
        self._loop = loop
        # No await separates submission from ownership registration. There is
        # exactly one submitted operation, never an executor backlog.
        worker = self._executor.submit(function, *args)
        waiter = asyncio.wrap_future(worker, loop=loop)
        waiter.add_done_callback(_observe_wrapper_failure)
        self._pending = _Pending(operation, worker, waiter, closing)
        self.last_failure = None
        return await self._observe()

    async def _observe(self):
        if self._observing:
            raise StoreRefused("Another task is already observing the owned storage operation")
        if asyncio.get_running_loop() is not self._loop:
            raise StoreRefused("Storage settlement must use the owning event loop")
        self._observing = True
        pending = self._pending
        try:
            # asyncio.wait neither cancels the waiter nor the original concurrent
            # future. The latter remains the actual completion authority.
            done, _unfinished = await asyncio.wait({pending.waiter}, timeout=self._seconds)
            if not done:
                failure = StoreFailed(
                    f"Waiting to {pending.operation} exceeded its time limit; I/O remains owned and may still finish; settle before reuse"
                )
                self.last_failure = failure
                raise failure
            self._pending = None
            try:
                return pending.worker.result()
            except StoreRefused:
                raise
            except BaseException as cause:
                if pending.closing:
                    self._close_failed = True
                failure = (
                    cause
                    if isinstance(cause, StoreFailed)
                    else StoreFailed(f"Cannot {pending.operation}; inspect the protected storage cause", cause=cause)
                )
                self.last_failure = failure
                raise failure from None
            finally:
                if pending.closing:
                    self._executor.shutdown(wait=False)
                    self._closed = True
        except asyncio.CancelledError as cause:
            self.last_failure = StoreFailed(
                f"Waiting to {pending.operation} was cancelled; the original I/O remains owned; settle before reuse", cause=cause
            )
            raise
        finally:
            self._observing = False
