"""Request fixed host-owned worker roles without granting engine authority.

The private parent-root handoff extends HostRetirement's qualified filesystem
protocol (1de1074) to creation. The host fixes images, commands, grants and
resources; this request contains only a closed role and fresh allocation token.
The factory must retain this owner before start(), retire abandoned/partial work
through retirement, then settle its original I/O before reporting clean refusal.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import os
import re
import stat
import time
from builtins import BaseExceptionGroup

from nmrnet_provider.retirement import HostRetirement, retire_worker_channel
from nmrnet_provider.worker_protocol import require_seconds


class AllocationUnconfirmed(RuntimeError):
    """Safe operation text; native/filesystem causes remain protected."""


def release_native_history(*, frontier, previous, request_root, receipt_root, seconds):
    """Publish one clean-service handoff and await its exact host frontier.

    The factory retains this original thread across interrupted observation.
    Receipt absence grants neither another publication nor deletion permission.
    """
    expected = f"released {frontier:032x}\n".encode("ascii")
    prior = f"released {previous:032x}\n".encode("ascii")
    try:
        staging = request_root / ".release-history-pending"
        descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            target.write(f"release {frontier:032x}\n".encode("ascii"))
        staging.replace(request_root / ".release-history")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            receipt = WorkerAllocation._read_path(receipt_root / ".history-released", 257)
            if receipt == expected:
                return frontier
            if receipt is not None and receipt != prior:
                raise AllocationUnconfirmed(
                    "Cannot confirm native history release: the host acknowledgement is malformed or has a different frontier."
                )
            time.sleep(0.02)
    except OSError as cause:
        raise AllocationUnconfirmed(
            "Cannot publish or observe native history release; preserve its original handoff and private filesystem cause."
        ) from cause
    raise AllocationUnconfirmed(
        "The host has not acknowledged native history release within its observation budget; preserve the original handoff and do not admit more work."
    )


class WorkerAllocation:
    """One publication attempt and retained observation, including late creation.

    start() may observe the same task again; it never issues another request.
    Cancellation/timeout of its waiter does not cancel that task or the host.
    settle() observes only local request I/O, never confirms native retirement.
    Parent roots are trusted composition grants, never Job/interpreter paths.
    """

    def __init__(self, *, role, allocation, request_root, receipt_root, ipc_root, operation_seconds):
        if role not in ("prediction", "reader", "smiles") or type(role) is not str:
            raise ValueError("Cannot request native allocation: role must be prediction, reader or smiles.")
        if type(allocation) is not str or re.fullmatch(r"[0-9a-f]{32}", allocation) is None:
            raise ValueError("Cannot request native allocation: a fresh 32-character lowercase hexadecimal token is required.")
        require_seconds(operation_seconds)
        self.role, self.allocation = role, allocation
        self._request = request_root / allocation
        self._receipt = receipt_root / allocation
        self.ipc = ipc_root / allocation
        self.metadata = None
        self._seconds = operation_seconds
        self.operation = None
        self._publication = None
        self._retirement = None
        self._consumption = None
        self._consumption_failure = None
        self._owns_leaf = False
        self.last_failure = None
        self._host_retirement = HostRetirement(
            allocation=allocation,
            request_directory=self._request,
            receipt_directory=self._receipt,
            operation_seconds=operation_seconds,
        )

    @property
    def retirement(self):
        # Returning this owner coordinates native retirement with its original
        # publication; a bare HostRetirement cannot own that earlier setup phase.
        return self

    async def start(self):
        """Return after host acceptance and channel readiness, never model readiness."""
        if self._retirement is not None:
            raise AllocationUnconfirmed("Native retirement has been requested; this allocation cannot be adopted or reconnected.")
        if self.operation is None:
            self._publication = asyncio.create_task(asyncio.to_thread(self._publish))
            self._publication.add_done_callback(self._observe)
            self.operation = asyncio.create_task(self._run())
            self.operation.add_done_callback(self._observe)
        accepted = await self._wait(self.operation)
        if not accepted or self._retirement is not None:
            raise AllocationUnconfirmed(
                "The requested worker was retired before adoption; preserve its receipts and do not reconnect."
            )

    async def settle(self):
        """Observe the original publication/read task; issue no native command."""
        if self.operation is not None:
            await self._wait(self.operation)

    async def retire(self):
        """Revoke adoption, settle original publication, then retire that exact leaf."""
        previous = self._retirement
        if previous is None or (previous.done() and (previous.cancelled() or previous.exception() is not None)):
            self._retirement = asyncio.create_task(self._retire())
            self._retirement.add_done_callback(self._observe)
        await self._wait(self._retirement)

    async def _retire(self):
        if self._publication is None:
            return
        failures = []
        try:
            await asyncio.shield(self._publication)
        except Exception as cause:
            failures.append(cause)
        if self._owns_leaf:
            try:
                await self._host_retirement.retire()
            except (Exception, asyncio.CancelledError) as cause:
                failures.append(cause)
        try:
            await asyncio.shield(self.operation)
        except Exception as cause:
            if not any(previous is cause for previous in failures):
                failures.append(cause)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Native allocation setup and retirement could not both finish", failures)

    async def retire_channel(self, worker):
        """Renew capacity only after original setup, host and channel settlement.

        A consumption task exists only after all three have succeeded. Later
        calls observe that handoff, without closing or retiring the worker again.
        Failed/unadopted factory setup deliberately uses host retirement alone.
        """
        if self._consumption is None:
            failures = await retire_worker_channel(worker, self)
            if failures:
                return failures
        try:
            await self._confirm_consumed()
        except (Exception, asyncio.CancelledError) as cause:
            return [("allocation_consumption", cause)]
        return []

    async def _confirm_consumed(self):
        if self._publication is None:
            return
        if self._consumption_failure is not None and (self._consumption is None or self._consumption.done()):
            raise self._consumption_failure
        original = self._consumption
        publish = original is None
        if original is not None and original.done():
            if original.cancelled():
                raise AllocationUnconfirmed(
                    "Native capacity observation was cancelled internally; preserve its original handoff."
                )
            failure = original.exception()
            if failure is None:
                return
            if type(failure) is not AllocationUnconfirmed:
                raise failure
            # Only a completed missing-receipt deadline reaches here without
            # the permanent failure latch. Read again; never republish consumed.
        if original is None or original.done():
            self._consumption = asyncio.create_task(asyncio.to_thread(self._consume_archive, publish))
            self._consumption.add_done_callback(self._observe)
        try:
            async with asyncio.timeout(self._seconds):
                await asyncio.shield(self._consumption)
        except TimeoutError as cause:
            raise AllocationUnconfirmed(
                "Native retirement is confirmed, but capacity renewal exceeded its waiting budget; preserve the original consumption task and archive evidence."
            ) from cause

    def _consume_archive(self, publish):
        if publish:
            try:
                staging = self._request / "consumption"
                descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as target:
                    target.write(f"consumed {self.allocation}\n".encode("ascii"))
                # Keep the completed staging link: once consumed is visible the
                # host may rename this entire request tree, before this returns.
                os.link(staging, self._request / "consumed", follow_symlinks=False)
            except OSError as cause:
                failure = AllocationUnconfirmed(
                    "Cannot confirm native consumption publication; preserve the retired allocation, original I/O and private filesystem cause."
                )
                self._consumption_failure = failure
                raise failure from cause
        deadline = time.monotonic() + self._seconds
        while time.monotonic() < deadline:
            try:
                receipt = self._read("archived")
                if receipt == f"archived {self.allocation}\n".encode("ascii"):
                    return
                if receipt is not None:
                    raise AllocationUnconfirmed(
                        "Host archive receipt is malformed or belongs to another allocation; capacity renewal is unconfirmed."
                    )
            except (OSError, AllocationUnconfirmed) as cause:
                failure = AllocationUnconfirmed(
                    "Cannot safely observe native capacity renewal; preserve the retired allocation, receipt and private cause, including descriptor cleanup."
                )
                self._consumption_failure = failure
                raise failure from cause
            time.sleep(0.02)
        raise AllocationUnconfirmed(
            "The host has not confirmed native capacity renewal within the observation budget; retirement remains confirmed, and a later observation may read the archive receipt without resending consumption."
        )

    async def _wait(self, operation):
        try:
            async with asyncio.timeout(self._seconds):
                return await asyncio.shield(operation)
        except TimeoutError as cause:
            raise AllocationUnconfirmed(
                "Cannot confirm native allocation within its waiting budget. Preserve this request, retire its exact allocation and settle the original I/O before replacement."
            ) from cause

    def _observe(self, operation):
        # A cancelled factory waiter must not print private filesystem evidence
        # through an abandoned task's event-loop diagnostic.
        if not operation.cancelled():
            failure = operation.exception()
            if failure is not None:
                self.last_failure = failure

    def _publish(self):
        try:
            self._request.mkdir(mode=0o700)
            self._owns_leaf = True
            descriptor = os.open(self._request / "pending", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                target.write(f"allocate {self.role} {self.allocation}\n".encode("ascii"))
            # The host sees a complete closed request, never its partial write.
            # link refuses replacement; failed local cleanup remains observable.
            os.link(self._request / "pending", self._request / "allocate", follow_symlinks=False)
            (self._request / "pending").unlink()
        except OSError as cause:
            raise AllocationUnconfirmed(
                "Cannot publish native allocation. Preserve its private filesystem cause, original I/O and any owned leaf for retirement."
            ) from cause

    async def _run(self):
        await asyncio.shield(self._publication)
        return await asyncio.to_thread(self._observe_receipts)

    def _observe_receipts(self):
        try:
            deadline = time.monotonic() + self._seconds
            while time.monotonic() < deadline:
                retired = self._read("result")
                if retired == f"retired {self.allocation}\n".encode("ascii"):
                    return False
                if retired is not None:
                    raise AllocationUnconfirmed(
                        "Host retirement receipt is unconfirmed or malformed; preserve the requested allocation."
                    )
                receipt = self._read("allocated")
                if receipt == f"allocated {self.role} {self.allocation}\n".encode("ascii"):
                    ready = self._read_path(self.ipc / "ready", 257)
                    if ready == (self.allocation + "\n").encode("ascii"):
                        if self.role == "prediction":
                            self.metadata = self._read_path(self.ipc / "metadata.json", 4097)
                            if self.metadata is None or len(self.metadata) > 4096:
                                raise AllocationUnconfirmed(
                                    "Prediction channel has no bounded runtime metadata; preserve and retire its allocation."
                                )
                        return True
                    if ready is not None and not (self.allocation + "\n").encode("ascii").startswith(ready):
                        raise AllocationUnconfirmed(
                            "Native channel readiness belongs to another allocation; preserve the requested worker."
                        )
                    time.sleep(0.02)
                    continue
                if receipt is not None:
                    raise AllocationUnconfirmed(
                        "Host allocation receipt does not match the exact role/token; preserve the requested allocation."
                    )
                time.sleep(0.02)
        except OSError as cause:
            raise AllocationUnconfirmed(
                "Cannot publish or observe native allocation. Preserve its private filesystem cause, request and exact retirement obligation."
            ) from cause
        raise AllocationUnconfirmed(
            "The host did not confirm allocation within its observation budget; retire the exact request before replacement."
        )

    def _read(self, name):
        return self._read_path(self._receipt / name, 257)

    @staticmethod
    def _read_path(path, limit):
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise AllocationUnconfirmed("Host allocation receipt is not a regular file; no allocation is admitted.")
            return os.read(descriptor, limit)
        finally:
            os.close(descriptor)
