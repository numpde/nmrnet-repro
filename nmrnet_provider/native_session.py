"""Own asynchronous native setup, retirement and one lost-control proof session.

The core supervisor owns child/scratch facts and the shared session retains its
control operations. This binding adopts the science endpoint, retains its own
setup and closure tasks, and admits NMRNet startup observations. A proof session
issues no science command.
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
from provider_core.async_runner_session import AsyncRunnerSession, await_original
from provider_core.runner_protocol import LifecycleRejected
from provider_core.runner_session import RunnerSession, RunnerStartupStopped, RunnerStartupUnavailable
from provider_core.task_boundary import contain_task_exit


class NativeWorkerExited(RuntimeError):
    """Carry the original native owner to the Attempt's scientific session.

    This observation does not authorize FAIL. Core checks the analysis owner,
    acknowledgement, active phase and original exit before selecting a report.
    """

    def __init__(self, operation, cause):
        super().__init__("Native worker exited before local interruption; retain its original session and cause.")
        self.native_operation, self.cause = operation, cause

    @property
    def runner(self):
        return self.native_operation.session

    def retain_cleanup_failure(self, failure):
        """Preserve an independent close error without masking the observed exit."""
        previous = self.__cause__ if self.__cause__ is not None else self.cause
        self.__cause__ = BaseExceptionGroup("Native exit and cleanup failed", (previous, failure))


class NativeOperation:
    """Retain setup before its first await; transfer this owner with its client."""

    def __init__(self, *, role, endpoint, operation_seconds, observation_seconds, on_notice):
        if role not in ("prediction", "reader", "smiles"):
            raise ValueError("Native operation requires the fixed prediction, reader or smiles role.")
        require_seconds(operation_seconds)
        require_seconds(observation_seconds)
        if observation_seconds >= operation_seconds:
            raise ValueError("Native exit observation must leave time for stop and retirement.")
        if not callable(on_notice):
            raise ValueError("Native operation requires a protected lifecycle diagnostic recipient.")
        self._on_notice = on_notice
        self.role, self.endpoint, self._seconds = role, endpoint, operation_seconds
        self._observation_seconds = observation_seconds
        self.session = self.proof = None
        self._control = None
        self._proof_control = None
        self.reader = self.writer = None
        self.runtime = None
        self._opening = self._retirement = self._proof_task = None
        self._opening_interruption = None
        self._science_closure = None
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

    @property
    def _control_retirement(self):
        return None if self._control is None else self._control.reader

    def _observe(self, task):
        if not task.cancelled():
            task.exception()

    def _observe_opening(self, task):
        if task.cancelled():
            if self._opening_interruption is None:
                try:
                    task.result()
                except asyncio.CancelledError as cause:
                    self._opening_interruption = cause
        else:
            self._observe(task)

    @property
    def last_failure(self):
        failures = [] if self._stop_failure is None else [self._stop_failure]
        if self._opening_interruption is not None:
            failures.append(self._opening_interruption)
        if self._close_failure is not None:
            failures.append(self._close_failure)
        for control in (self._control, self._proof_control):
            if control is not None:
                for cause in (control.stop_failure, control.setup_failure, control.retirement_failure):
                    if cause is not None and all(cause is not earlier for earlier in failures):
                        failures.append(cause)
        for task in (self._opening, self._proof_task, self._science_closure, self._retirement):
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

    def _lease_identity(self):
        if not self.endpoint.is_absolute() or self.endpoint.parent.resolve() != self.endpoint.parent:
            raise ValueError("Native runner endpoint requires an admitted canonical absolute parent.")
        descriptor = os.open(self.endpoint.parent / ".owner-session.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            observed = os.fstat(descriptor)
            if not stat.S_ISREG(observed.st_mode) or observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o600:
                raise PermissionError("Native runner lease must be a caller-owned mode 0600 regular file.")
            return observed.st_dev, observed.st_ino
        finally:
            os.close(descriptor)

    async def _open(self):
        identity = await asyncio.to_thread(self._lease_identity)
        self.session = RunnerSession(
            self.endpoint, role=self.role, expected_lock_identity=identity,
            startup_seconds=self._seconds, retirement_seconds=self._seconds,
        )
        self._control = AsyncRunnerSession(self.session, not_after=self._cleanup_deadline)
        if self._stop_requested:
            self._control.request_stop()
        await self._control.connect(wait_seconds=self._seconds)
        if self._stop_requested:
            self._request_stop()
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
            self._opening = asyncio.create_task(contain_task_exit(self._open))
            self._opening.add_done_callback(self._observe_opening)
        cancellations = asyncio.current_task().cancelling()
        try:
            await self._wait(self._opening, time.monotonic() + self._seconds)
        except asyncio.CancelledError:
            if self._opening.cancelled() and asyncio.current_task().cancelling() == cancellations:
                self._observe_opening(self._opening)
                raise self._opening_interruption
            raise
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError) as cause:
            await self.observe_failure(cause)
            raise
        if self._stop_requested:
            raise LifecycleRejected("Native operation was retired during startup; do not adopt its channel.")
        return self.reader, self.writer

    def _request_stop(self):
        self.mark_interruption()
        if self.session is not None:
            try:
                self._control.request_stop()
            except OSError as cause:
                # A partial connect or lost control still needs the original
                # opening task and, where necessary, a retained proof session.
                if self._stop_failure is None:
                    self._stop_failure = cause
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception as cause:
                # Local close failure must not skip independent child retirement.
                self._close_failure = cause

    def mark_interruption(self):
        """Record intent before closing either of this operation's channels."""
        if self.session is not None:
            self.session.mark_interruption()
        self._stop_requested = True

    async def observe_failure(self, cause):
        """Observe the original runner after unexpected scientific channel loss.

        The same retained reader supplies cleanup later. An expired or failed
        observation remains with that task; it never proves a worker crash.
        """
        if self.session is None or self.session.operation is None:
            return
        try:
            await self._control.observe_retirement(wait_seconds=self._observation_seconds)
        except (OSError, LifecycleRejected):
            # Retirement observes this same task and reports its original error.
            # Cleanup can fail after an original exit was positively observed.
            if self.session.original_exit is None:
                return
        if self.session.original_exit is not None:
            raise NativeWorkerExited(self, cause) from cause

    async def _prove_retirement(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Native cleanup deadline expired before proof-session acquisition; ownership remains held.")
        self.proof = RunnerSession(
            self.endpoint, role=self.role, expected_lock_identity=self.session.expected_lock_identity,
            startup_seconds=remaining, retirement_seconds=remaining,
        )
        self._proof_control = AsyncRunnerSession(self.proof, not_after=deadline)
        try:
            await self._proof_control.connect(wait_seconds=remaining)
            self._proof_control.request_stop()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Native cleanup deadline expired before proof retirement; ownership remains held.")
            await self._proof_control.join_retirement(wait_seconds=remaining)
        except BaseException as primary:
            failures = []
            try:
                # Revoke first, but never close a control socket underneath its
                # original setup or retirement read. A late waiter remains held.
                self._proof_control.request_stop()
            except BaseException as cleanup:
                failures.append(cleanup)
            try:
                for task in (self._proof_control.setup, self._proof_control.reader):
                    if task is None:
                        continue
                    if task.cancelled():
                        raise RuntimeError("Native proof operation was cancelled; its physical retirement is unconfirmed.")
                    try:
                        await await_original(task, deadline=None)
                    except BaseException as cleanup:
                        if not task.done() or task.cancelled():
                            raise
                        if cleanup is not primary and all(cleanup is not item for item in failures):
                            failures.append(cleanup)
                self.proof.close()
            except BaseException as cleanup:
                failures.append(cleanup)
            if failures:
                raise BaseExceptionGroup("Native proof setup and local cleanup both failed", [primary, *failures]) from None
            raise

    async def _retire(self):
        deadline = self._cleanup_deadline
        if self._opening is not None:
            cancellations = asyncio.current_task().cancelling()
            try:
                await self._wait(self._opening, deadline)
            except asyncio.CancelledError:
                if not self._opening.cancelled() or asyncio.current_task().cancelling() > cancellations:
                    raise
                self._observe_opening(self._opening)
            except (RunnerStartupStopped, RunnerStartupUnavailable):
                # The control connection was never established. A proof session
                # would create a child instead of retiring an existing one.
                if self.session.control_connected:
                    raise
                self.session.close()
                self._report_settled_failures()
                return
            except (Exception, BaseExceptionGroup):
                if not self._opening.done():
                    raise
                # The retained opening task still carries this refusal. Its
                # failure does not remove our obligation to stop a partial child.
        if self.session is None:
            self._report_settled_failures()
            return
        if self._control.retirement_deadline is not None:
            deadline = min(deadline, self._control.retirement_deadline)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Native cleanup deadline expired before control retirement; ownership remains held.")
            await self._control.join_retirement(wait_seconds=remaining)
        except (RunnerStartupStopped, RunnerStartupUnavailable):
            # These setup outcomes prove no control connection was established.
            if self.session.control_connected:
                raise
            self.session.close()
            self._report_settled_failures()
            return
        except Exception:
            reader = self._control_retirement
            if reader is None:
                setup = self._control.setup
                if (setup is None or not setup.done() or setup.cancelled() or self.session.operation is None
                        or self._control.setup_failure is None):
                    raise
                needs_proof = True
            else:
                if not reader.done() or reader.cancelled():
                    raise
                needs_proof = self._control.retirement_failure is not None
            if needs_proof:
                self.session.control.close()
                if self._proof_task is None:
                    self._proof_task = asyncio.create_task(contain_task_exit(self._prove_retirement, deadline))
                    self._proof_task.add_done_callback(self._observe)
                await self._wait(self._proof_task, deadline)
        try:
            if self.writer is not None:
                if self._science_closure is None:
                    self._science_closure = asyncio.create_task(contain_task_exit(self.writer.wait_closed))
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
            if self._cleanup_deadline is None and self._control is None:
                self._cleanup_deadline = time.monotonic() + self._seconds
            self._request_stop()
            if self._cleanup_deadline is None:
                self._cleanup_deadline = self._control.retirement_deadline
            self._retirement = asyncio.create_task(contain_task_exit(self._retire))
            self._retirement.add_done_callback(self._observe)
        await self._wait(self._retirement, time.monotonic() + self._seconds)

    async def settle(self):
        if self._retirement is None:
            raise RuntimeError("Retire native operation before settling its local I/O and grants.")
        await self._wait(self._retirement, time.monotonic() + self._seconds)

    async def retire_channel(self, worker):
        failures = []
        # Closing science can make the child exit before the control stop arrives.
        self.mark_interruption()
        try:
            worker.close()
        except (Exception, BaseExceptionGroup) as cause:
            failures.append(("channel_close", cause))
        try:
            await self.retire()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            failures.append(("native_retirement", cause))
        # This operation already observes the client's exact StreamWriter.
        # Rejoining it through worker.wait_closed would count the same settled
        # transport error again and incorrectly retain a retired operation.
        return failures
