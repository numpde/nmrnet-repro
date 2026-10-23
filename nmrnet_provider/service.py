"""Own the provider's service tasks around the existing Attempt controller.

Ordering adapts NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/provider_process.py:192-214,263-320; independent failure/join
discipline follows SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/runtime.py:113-140. This async owner adds no engine
access, scientific dispatch, recovery journal or second Attempt state machine.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import math
import random
from builtins import BaseExceptionGroup
from dataclasses import dataclass, field
from enum import Enum, auto

from nmrnet_provider.admission import AdmissionPolicy, archive_confirmed_history, require_admission_ready, select_pending_start
from nmrnet_provider.config import ANALYSIS_KIND_REF
from provider_core.api import ApiFailure, ProviderAPI
from provider_core.hello import AnalysisOffering


@dataclass(frozen=True, slots=True)
class Announcement:
    display_name: str = field(repr=False)
    description: str = field(repr=False)
    analysis_offerings: tuple[AnalysisOffering, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ServicePolicy:
    admission: AdmissionPolicy
    idle_seconds: float
    hello_seconds: float
    drain_seconds: float
    settle_seconds: float
    api_backoff_max_seconds: float

    def __post_init__(self):
        if type(self.admission) is not AdmissionPolicy:
            raise ValueError("Cannot configure NMRNet service: an admitted paging policy is required.")
        for value in (
            self.idle_seconds,
            self.hello_seconds,
            self.drain_seconds,
            self.settle_seconds,
            self.api_backoff_max_seconds,
        ):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(
                    "Cannot configure NMRNet service: cadence, backoff, drain and settlement waits must be finite positive seconds."
                )
        if self.api_backoff_max_seconds < self.idle_seconds:
            raise ValueError("Cannot configure NMRNet service: API backoff maximum must be at least idle_seconds.")


class ServiceRefused(RuntimeError):
    """No new service invocation or ownership transfer is authorized."""


class ServiceFailed(RuntimeError):
    """Safe operation context; original and independent failures stay protected."""

    def __init__(self, failures):
        super().__init__(
            f"NMRNet service could not finish {failures[0][0]}. Preserve retained work and this service; "
            "inspect its protected failures and settle original tasks before releasing dependencies or restarting."
        )
        self.failures = failures

    @property
    def cause(self):
        return self.failures[0][1]


class _State(Enum):
    NEW = auto()
    RUNNING = auto()
    HELD = auto()
    SETTLING = auto()
    RELEASED = auto()


def _retrieve_task_failure(task):
    # Retain the task/result for explicit observation without letting an abandoned
    # wait print private API, source, factory or scientific evidence to the loop.
    if not task.cancelled():
        task.exception()


class ProviderService:
    """One serial Attempt lane and independently paced current announcements.

    The caller owns the opened store and lifecycle API. open_attempt owns partial
    allocation and setup I/O until it returns the existing controller, or confirms
    their cleanup before raising. Its original invocation stays in work_task.
    Successful return transfers the controller here before shutdown is checked.
    Factory cleanup failure must remain an explicit caller/host-owned obligation.
    work_task, controller and their failures are protected diagnostic owners;
    their representations are not public status messages.

    on_started means initial admission/Hello completed, not scientific readiness.
    current_announcement and on_started are bounded synchronous trusted callbacks.
    on_api_retry and on_api_recovered are bounded synchronous diagnostic observers.
    Empty offerings pause fresh allocation. Work already handed to the factory
    remains owned until it finishes or stop is requested. Reviewed admission and
    discovery read failures retry from a fresh complete view. Hello recovery
    builds the latest complete announcement after each wait. Active Attempt
    failures and holds still stop; other mutations/science never use these loops.
    Retry/recovery callbacks receive the operation kind and protected failures.
    """

    def __init__(
        self,
        *,
        config,
        store,
        api,
        open_attempt,
        release_native_history,
        current_announcement,
        policy,
        on_started,
        on_api_retry,
        on_api_recovered,
    ):
        if type(policy) is not ServicePolicy or not all(
            map(
                callable, (open_attempt, release_native_history, current_announcement, on_started, on_api_retry, on_api_recovered)
            )
        ):
            raise ValueError(
                "Cannot configure NMRNet service: explicit policy, allocation factory, announcement supplier and lifecycle/diagnostic callbacks are required."
            )
        self.config, self.store, self.api = config, store, api
        self._open_attempt, self._announcement, self._on_started = open_attempt, current_announcement, on_started
        self._release_native_history = release_native_history
        self._on_api_retry, self._on_api_recovered = on_api_retry, on_api_recovered
        self.policy = policy
        self.hello_api = ProviderAPI(
            config.api.identity, config.api.http.new_transport(), expected_topology=config.api.expected_topology
        )
        self.controller = None
        self.work_task = None
        self._hello_task = None
        self._settlement_task = None
        self._maintenance_task = None
        self._expected_cancellations = set()
        self._failures = []
        self.last_failure = None
        self.unconfirmed_hello = None
        self._state = _State.NEW
        self._stop = None

    async def run(self, stop):
        """Run once; stop prohibits new allocation before bounded drain/settlement."""
        if self._state is not _State.NEW:
            raise ServiceRefused(
                "This service accepts one run. Settle its original tasks and retained obligations before arranging a replacement."
            )
        if not isinstance(stop, asyncio.Event):
            raise ValueError("NMRNet service requires an explicit asyncio stop event.")
        self._state, self._stop = _State.RUNNING, stop
        cancellation = None
        clean = False
        try:
            if not stop.is_set() and await self._startup():
                self._hello_task = self._start_task(self._hello_loop())
                self.work_task = self._start_task(self._work_loop())
                await self._watch()
        except asyncio.CancelledError as cause:
            cancellation = cause
            self._remember("the service invocation", cause)
        except (Exception, BaseExceptionGroup) as cause:
            self._remember("startup or service observation", cause)
        finally:
            stop.set()
            try:
                clean = await self._shutdown(drain=True, retry_settlement=False)
            except asyncio.CancelledError as cause:
                self._remember("shutdown observation", cause)
                if cancellation is None:
                    cancellation = cause
            except (Exception, BaseExceptionGroup) as cause:
                self._remember("shutdown observation", cause)
            finally:
                self._state = _State.RELEASED if clean else _State.HELD
        if cancellation is not None:
            raise cancellation
        if self.last_failure is not None:
            raise self.last_failure from None

    async def _startup(self):
        await self._archive_completed()
        await self._read_until_available(self._admission)
        if self._stop.is_set():
            return False
        await self._hello_until_available()
        if self._stop.is_set():
            return False
        self._on_started()
        return not self._stop.is_set()

    async def _archive_completed(self):
        if self._stop.is_set():
            return
        self._maintenance_task = self._start_task(self._archive_release_trim())
        stopped = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait({self._maintenance_task, stopped}, return_when=asyncio.FIRST_COMPLETED)
            if self._maintenance_task.done():
                self._maintenance_task.result()
                self._maintenance_task = None
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)

    async def _archive_release_trim(self):
        """One admitted batch finishes its ordered phases during shutdown.

        Stop before the inventory gate admits nothing. After it, shutdown owns
        this original task; cancellation must not strand the transient window.
        """
        archived = await archive_confirmed_history(
            self.config, self.api, self.store, policy=self.policy.admission, owner=self, stop=self._stop
        )
        if archived:
            await self._release_native_history()
            await self.store.trim_confirmed_history(owner=self)

    async def _admission(self):
        await require_admission_ready(self.config, self.api, self.store, policy=self.policy.admission, owner=self)

    async def _select_after_admission(self):
        """Observe the complete live view before starting a fresh discovery walk."""
        await self._admission()
        if self._stop.is_set():
            return None
        return await select_pending_start(self.config, self.api, policy=self.policy.admission, owner=self)

    async def _read_until_available(self, operation):
        """Repeat only the supplied admission/read cycle, in its original task.

        Each failed page abandons the whole view. No allocation or mutation is
        inside this boundary. Backoff resets after the complete cycle succeeds;
        each request remains bounded and the outage itself may wait until stop.
        """
        return await self._until_api_available(operation, api=self.api, permit_retry=self.api.can_retry_read, kind="read")

    async def _hello_until_available(self):
        return await self._until_api_available(
            self._publish_current, api=self.hello_api, permit_retry=self.hello_api.can_refresh_hello, kind="hello"
        )

    async def _until_api_available(self, operation, *, api, permit_retry, kind):
        """Pace only the two operation-owned permissions, after original I/O settles.

        The read caller repeats its whole view; Hello constructs current state.
        This owner never retains request bytes or authorizes another mutation.
        """
        delay, outage = self.policy.idle_seconds, None
        while not self._stop.is_set():
            try:
                result = await operation()
            except ApiFailure as failure:
                try:
                    await api.retire(owner=self)
                except (Exception, asyncio.CancelledError, BaseExceptionGroup):
                    self._remember(f"API {kind} before failed transport retirement", failure)
                    raise
                if not permit_retry(failure):
                    raise
                if self._stop.is_set():
                    return None
                outage = failure
                wait = random.uniform(delay / 2, delay)
                self._on_api_retry(kind, failure, wait)
                if await self._pause(wait):
                    return None
                delay = min(self.policy.api_backoff_max_seconds, delay * 2)
            else:
                if outage is not None:
                    self._on_api_recovered(kind, outage)
                return result
        return None

    def _snapshot(self):
        snapshot = self._announcement()
        if type(snapshot) is not Announcement:
            raise ValueError("Cannot announce NMRNet: the current supplier must return a complete Announcement.")
        if any(item.analysis_kind_ref != ANALYSIS_KIND_REF for item in snapshot.analysis_offerings):
            raise ValueError("Cannot announce NMRNet: only the implemented peaks_from_mol offering may be published.")
        return snapshot

    async def _publish_current(self):
        snapshot = self._snapshot()
        try:
            await self.hello_api.publish_hello(
                provider_ref=self.config.lifecycle.scope.provider_ref,
                display_name=snapshot.display_name,
                description=snapshot.description,
                analysis_offerings=snapshot.analysis_offerings,
                owner=self,
            )
        except (ApiFailure, asyncio.CancelledError):
            # Capture at the send owner: startup has no Hello task, and a later
            # interrupted send must replace an earlier outage's uncertainty.
            self.unconfirmed_hello = self.hello_api.last_failure
            raise
        else:
            # This receipt confirms this current snapshot, not any prior send.
            self.unconfirmed_hello = None

    async def _hello_loop(self):
        while not await self._pause(self.policy.hello_seconds):
            await self._hello_until_available()

    async def _work_loop(self):
        while not self._stop.is_set():
            if not self._snapshot().analysis_offerings:
                await self._pause(self.policy.idle_seconds)
                continue
            # Reaching another cycle means the prior controller returned and
            # released its claim. Archive outside every API read-retry boundary.
            await self._archive_completed()
            pending = await self._read_until_available(self._select_after_admission)
            if self._stop.is_set():
                return
            if pending is None:
                await self._pause(self.policy.idle_seconds)
                continue
            if not self._snapshot().analysis_offerings:
                # Withdrawal can occur during either network walk. Discard this
                # selection; resumption must start with fresh observations.
                await self._pause(self.policy.idle_seconds)
                continue
            self.controller = await self._open_attempt(pending)
            # Adoption precedes this stop check, including a late factory return.
            # Shutdown observes work_task before settling that exact controller.
            if self._stop.is_set():
                return
            await self.controller.run(pending)

    async def _pause(self, seconds):
        try:
            async with asyncio.timeout(seconds):
                await self._stop.wait()
            return True
        except TimeoutError:
            return False

    async def _watch(self):
        stopped = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait({self.work_task, self._hello_task, stopped}, return_when=asyncio.FIRST_COMPLETED)
            # Record the initiating failure before shutdown can cancel another
            # task and add its independent cleanup/interruption observations.
            self._record_task_failure(self.work_task, "Attempt work")
            self._record_task_failure(self._hello_task, "current Hello publication")
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)

    def _start_task(self, operation):
        task = asyncio.create_task(operation)
        task.add_done_callback(_retrieve_task_failure)
        return task

    def _cancel_once(self, task):
        if task is not None and not task.done() and task not in self._expected_cancellations:
            self._expected_cancellations.add(task)
            task.cancel()

    async def _wait_task(self, task, seconds):
        if task is None or task.done():
            return True
        done, _ = await asyncio.wait({task}, timeout=seconds)
        return bool(done)

    def _record_task_failure(self, task, operation):
        if not (task is not None and task.done()):
            return
        # Factory setup can group cancellation with failed cleanup. Keep the
        # original group intact while independent service cleanup proceeds.
        try:
            task.result()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            if isinstance(cause, asyncio.CancelledError) and task in self._expected_cancellations and cause.__cause__ is None:
                return
            self._remember(operation, cause)

    async def _shutdown(self, *, drain, retry_settlement):
        self._cancel_once(self._hello_task)
        failures = []
        for operation, function in (
            ("work task retirement", lambda: self._finish_work(drain)),
            ("Hello task retirement", self._finish_hello),
        ):
            failure = await self._cleanup(operation, function)
            if failure is not None:
                failures.append(failure)
        self._record_task_failure(self.work_task, "Attempt work")
        self._record_task_failure(self._hello_task, "current Hello publication")
        failure = await self._cleanup("confirmed-history maintenance", lambda: self._finish_maintenance(drain))
        if failure is not None:
            failures.append(failure)
        if (self.work_task is None or self.work_task.done()) and self._maintenance_task is None:
            for operation, function in (
                ("Attempt settlement", lambda: self._settle_controller(retry=retry_settlement)),
                ("execution API retirement", self._retire_api),
                ("preflight storage settlement", self._settle_store),
            ):
                failure = await self._cleanup(operation, function)
                if failure is not None:
                    failures.append(failure)
        if self._hello_task is None or self._hello_task.done():
            failure = await self._cleanup("Hello API retirement", lambda: self.hello_api.retire(owner=self))
            if failure is not None:
                failures.append(failure)
        # Interruption is still interruption, after every independent obligation
        # was attempted. The same errors remain protected in last_failure.
        for failure in failures:
            if isinstance(failure, asyncio.CancelledError):
                raise failure
        return not failures

    async def _finish_maintenance(self, drain):
        original = self._maintenance_task
        if original is None:
            return
        if drain:
            await self._wait_task(original, self.policy.drain_seconds)
        if not await self._wait_task(original, self.policy.settle_seconds):
            raise ServiceRefused(
                "Original confirmed-history maintenance is still pending; retain its task, native owner and storage dependencies before replacement."
            )
        self._record_task_failure(original, "confirmed-history maintenance")
        self._maintenance_task = None

    async def _finish_work(self, drain):
        try:
            if drain:
                await self._wait_task(self.work_task, self.policy.drain_seconds)
        finally:
            self._cancel_once(self.work_task)
        if not await self._wait_task(self.work_task, self.policy.settle_seconds):
            raise ServiceRefused(
                "Original service work is still pending. Retain its task and allocation before releasing dependencies or arranging a replacement."
            )

    async def _finish_hello(self):
        if not await self._wait_task(self._hello_task, self.policy.settle_seconds):
            raise ServiceRefused(
                "Original Hello work is still pending. Retain its API transport and service before releasing dependencies."
            )

    async def _settle_controller(self, *, retry):
        if self.controller is None:
            return
        previous = self._settlement_task
        if previous is None or (retry and previous.done() and (previous.cancelled() or previous.exception() is not None)):
            self._settlement_task = self._start_task(self.controller.settle())
        if not await self._wait_task(self._settlement_task, self.policy.settle_seconds):
            raise ServiceRefused(
                "The original controller settlement is still pending. Retain its task and allocation before retrying observation."
            )
        self._settlement_task.result()

    async def _retire_api(self):
        if self.api.pending_owner is not None and self.api.pending_owner is not self:
            raise ServiceRefused(
                "Execution API still belongs to another operation; settle its original owner before releasing dependencies."
            )
        await self.api.retire(owner=self)

    async def _settle_store(self):
        if self.store.pending_operation is not None:
            if self.store.pending_owner is not self:
                raise ServiceRefused(
                    "Storage still belongs to another setup or Attempt operation; its original owner must settle it."
                )
            await self.store.settle(owner=self)

    async def _cleanup(self, operation, function):
        try:
            await function()
        except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
            self._remember(operation, cause)
            return cause
        return None

    async def settle(self):
        """Observe original tasks/owners; never allocate, start, replay or rerun work."""
        if self._state in (_State.RUNNING, _State.SETTLING):
            raise ServiceRefused("The service already has an observer. Await its original invocation before settlement.")
        if self._state is _State.RELEASED:
            return
        self._state = _State.SETTLING
        try:
            clean = await self._shutdown(drain=False, retry_settlement=True)
            if not clean:
                raise self.last_failure from None
            self._state = _State.RELEASED
        finally:
            if self._state is _State.SETTLING:
                self._state = _State.HELD

    def _remember(self, operation, cause):
        if not any(name == operation and previous is cause for name, previous in self._failures):
            self._failures.append((operation, cause))
            self.last_failure = ServiceFailed(tuple(self._failures))
