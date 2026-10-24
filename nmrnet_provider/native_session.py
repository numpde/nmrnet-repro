"""Own asynchronous native setup, retirement and one lost-control proof session.

The core supervisor owns child/scratch facts. This binding retains original I/O
tasks across cancelled waiters, adopts the independent science endpoint, and
admits NMRNet startup observations. A proof session issues no science command.
See docs/notes/021_compose_runner_architecture_20261023.html for the serial
generation/lease barrier; no Engine authority or second lifetime registry lives here.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import os
import stat
import time

from nmrnet_provider.worker_protocol import document, frame_size, require_seconds
from provider_core.runner_protocol import LifecycleRejected
from provider_core.runner_session import RunnerSession


class NativeOperation:
    """Retain setup before its first await; transfer this owner with its client."""

    def __init__(self, *, role, endpoint, operation_seconds, on_notice):
        if role not in ("prediction", "reader", "smiles"):
            raise ValueError("Native operation requires the fixed prediction, reader or smiles role.")
        require_seconds(operation_seconds)
        if not callable(on_notice):
            raise ValueError("Native operation requires a protected lifecycle diagnostic recipient.")
        self._on_notice = on_notice
        self.role, self.endpoint, self._seconds = role, endpoint, operation_seconds
        self.session = self.proof = None
        self.reader = self.writer = None
        self.runtime = None
        self._opening = self._retirement = self._proof_task = None
        self._control_retirement = self._science_closure = None
        self._stop_failure = None
        self._close_failure = None
        self._stop_requested = False
        self._cleanup_deadline = None

    @property
    def allocation(self):
        return None if self.session is None else self.session.operation

    @property
    def retirement(self):
        return self

    def _observe(self, task):
        if not task.cancelled():
            task.exception()

    @property
    def last_failure(self):
        failures = [] if self._stop_failure is None else [self._stop_failure]
        if self._close_failure is not None:
            failures.append(self._close_failure)
        for task in (self._opening, self._control_retirement, self._proof_task, self._science_closure, self._retirement):
            if task is not None and task.done() and not task.cancelled():
                cause = task.exception()
                if cause is not None and all(cause is not earlier for earlier in failures):
                    failures.append(cause)
        if len(failures) == 1:
            return failures[0]
        if failures:
            return BaseExceptionGroup("Native lifecycle retained independent failures", failures)
        return None

    async def _wait(self, task, deadline):
        async with asyncio.timeout(max(0, deadline - time.monotonic())):
            return await asyncio.shield(task)

    def _connect(self):
        if not self.endpoint.is_absolute() or self.endpoint.parent.resolve() != self.endpoint.parent:
            raise ValueError("Native runner endpoint requires an admitted canonical absolute parent.")
        descriptor = os.open(self.endpoint.parent / ".owner-session.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o600:
                raise PermissionError("Native runner lease must be a caller-owned mode 0600 regular file.")
            identity = observed.st_dev, observed.st_ino
        finally:
            os.close(descriptor)
        self.session = RunnerSession(
            self.endpoint, role=self.role, expected_lock_identity=identity,
            startup_seconds=self._seconds, retirement_seconds=self._seconds,
        )
        self.session.connect()

    async def _open(self):
        await asyncio.to_thread(self._connect)
        if self._stop_requested:
            self.session.request_stop()
            return
        self.session.science.setblocking(False)
        self.reader, self.writer = await asyncio.open_connection(sock=self.session.science)
        # Successful asyncio adoption transfers the descriptor to its transport.
        # The control owner must not close that socket behind a pending waiter.
        self.session.science = None
        if self._stop_requested:
            # Retirement may begin while asyncio adopts the descriptor. Close
            # the newly returned transport before joining its original closure.
            self._request_stop()
            return
        if self.role == "prediction":
            async with asyncio.timeout(self._seconds):
                size = frame_size(await self.reader.readexactly(4))
                startup = document(await self.reader.readexactly(size))
            if (
                set(startup) != {"kind", "allocation", "runtime"} or startup["kind"] != "startup"
                or startup["allocation"] != self.allocation or type(startup["runtime"]) is not dict
            ):
                raise LifecycleRejected("Prediction startup does not contain this operation's observed runtime.")
            self.runtime = startup["runtime"]

    async def start(self):
        if self._stop_requested:
            raise LifecycleRejected("Native operation is retiring; its scientific endpoint cannot be adopted.")
        if self._opening is None:
            self._opening = asyncio.create_task(self._open())
            self._opening.add_done_callback(self._observe)
        await self._wait(self._opening, time.monotonic() + self._seconds)
        if self._stop_requested:
            raise LifecycleRejected("Native operation was retired during startup; do not adopt its channel.")
        return self.reader, self.writer

    async def connect_for_retirement(self):
        """Acquire the runner's serial barrier without adopting scientific I/O.

        A greeting follows retirement of the preceding session. This new proof
        child must itself retire through this operation before the caller reuses
        the runner; its exit status says nothing about historical science.
        """
        if self._opening is not None or self._stop_requested:
            raise LifecycleRejected("Retirement barrier requires an unused native operation.")
        self._opening = asyncio.create_task(asyncio.to_thread(self._connect))
        self._opening.add_done_callback(self._observe)
        await self._wait(self._opening, time.monotonic() + self._seconds)

    def _request_stop(self):
        self._stop_requested = True
        if self.session is not None:
            try:
                self.session.request_stop()
            except OSError as cause:
                # A partial connect or lost control still needs the original
                # opening task and, where necessary, a retained proof session.
                self._stop_failure = cause
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception as cause:
                # Local close failure must not skip independent child retirement.
                self._close_failure = cause

    def _prove_retirement(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Native cleanup deadline expired before proof-session acquisition; ownership remains held.")
        self.proof = RunnerSession(
            self.endpoint, role=self.role, expected_lock_identity=self.session.expected_lock_identity,
            startup_seconds=remaining, retirement_seconds=remaining,
        )
        try:
            self.proof.connect(deadline=deadline)
            self.proof.retire(deadline=deadline)
        except BaseException as primary:
            try:
                # Closing control revokes even an incompletely greeted child.
                # It does not substitute for missing retirement confirmation.
                self.proof.close()
            except BaseException as cleanup:
                raise BaseExceptionGroup("Native proof setup and local cleanup both failed", [primary, cleanup]) from None
            raise

    async def _retire(self):
        deadline = self._cleanup_deadline
        if self._opening is not None:
            try:
                await self._wait(self._opening, deadline)
            except Exception:
                if not self._opening.done():
                    raise
                # The retained opening task still carries this refusal. Its
                # failure does not remove our obligation to stop a partial child.
        if self.session is None:
            self._report_settled_failures()
            return
        try:
            if self._control_retirement is None:
                self._control_retirement = asyncio.create_task(asyncio.to_thread(self.session.retire, deadline=deadline))
                self._control_retirement.add_done_callback(self._observe)
            await self._wait(self._control_retirement, deadline)
        except Exception:
            if not self._control_retirement.done():
                raise
            self.session.control.close()
            if self._proof_task is None:
                self._proof_task = asyncio.create_task(asyncio.to_thread(self._prove_retirement, deadline))
                self._proof_task.add_done_callback(self._observe)
            await self._wait(self._proof_task, deadline)
        try:
            if self.writer is not None:
                if self._science_closure is None:
                    self._science_closure = asyncio.create_task(self.writer.wait_closed())
                    self._science_closure.add_done_callback(self._observe)
                try:
                    await self._wait(self._science_closure, deadline)
                except Exception as cause:
                    if (not self._science_closure.done() or self._science_closure.cancelled()
                            or self._science_closure.exception() is not cause):
                        raise
                    # asyncio's closed waiter can carry connection_lost's
                    # error after the transport has closed. Retain that cause
                    # for protected reporting; it is not unfinished closure.
        finally:
            try:
                self.session.close()
            finally:
                if self.proof is not None:
                    self.proof.close()
        self._report_settled_failures()

    def _report_settled_failures(self):
        failure = self.last_failure
        if failure is not None:
            # Settled resources can be transferred/released, but independent
            # setup and cleanup failures must still reach protected output.
            self._on_notice(failure, {"event": "native_lifecycle_settled", "role": self.role,
                                      "operation": self.allocation})

    async def retire(self):
        if self._retirement is None or (
            self._retirement.done() and not self._retirement.cancelled() and self._retirement.exception() is not None
        ):
            # Stop before joining opening/scientific I/O: a blocked worker or
            # opening thread must not prevent its owner from revoking the session.
            if self._cleanup_deadline is None:
                self._cleanup_deadline = time.monotonic() + self._seconds
            self._request_stop()
            self._retirement = asyncio.create_task(self._retire())
            self._retirement.add_done_callback(self._observe)
        await self._wait(self._retirement, time.monotonic() + self._seconds)

    async def settle(self):
        if self._retirement is None:
            raise RuntimeError("Retire native operation before settling its local I/O and grants.")
        await self._wait(self._retirement, time.monotonic() + self._seconds)

    async def retire_channel(self, worker):
        failures = []
        try:
            worker.close()
        except Exception as cause:
            failures.append(("channel_close", cause))
        try:
            await self.retire()
        except (Exception, asyncio.CancelledError) as cause:
            failures.append(("native_retirement", cause))
        # This operation already observes the client's exact StreamWriter.
        # Rejoining it through worker.wait_closed would count the same settled
        # transport error again and incorrectly retain a retired operation.
        return failures
