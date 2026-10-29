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
import logging
import math
import random
from builtins import BaseExceptionGroup
from dataclasses import dataclass, field
from enum import Enum, auto

from nmrnet_provider.admission import AdmissionPolicy, require_admission_ready, select_pending_start
from nmrnet_provider.config import ANALYSIS_KIND_REF
from provider_core.api import ApiFailure, ProviderAPI
from provider_core.controller import ControllerFailed
from provider_core.delivery_client import DeliveryClient, DeliveryUnavailable
from provider_core.delivery_store import DeliveryFull
from provider_core.execution import AttemptExecutor, ExecutionFailed
from provider_core.hello import AnalysisOffering
from provider_core.ownership import AttemptClaims, LiveCapacityFull
from provider_core.task_boundary import contain_task_exit
from provider_core.uploads import UploadRecoveryExhausted


LOGGER = logging.getLogger(__name__)


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

    The caller owns the provider lifetime lock and admission API. open_attempt owns partial
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
    builds the latest complete announcement after each wait. A settled,
    classified source outage retains its Job while unrelated work may proceed;
    unexpected Attempt failures stop the service. Other mutations/science never
    use these loops.
    Retry/recovery callbacks receive the operation kind and protected failures.
    """

    def __init__(
        self,
        *,
        config,
        api,
        open_attempt,
        claims,
        current_announcement,
        policy,
        on_started,
        on_api_retry,
        on_api_recovered,
        on_observed_crash,
        factory_ready,
        on_source_exhausted,
    ):
        if type(policy) is not ServicePolicy or not all(
            map(
                callable, (open_attempt, current_announcement, on_started, on_api_retry, on_api_recovered,
                           on_observed_crash, factory_ready, on_source_exhausted)
            )
        ):
            raise ValueError(
                "Cannot configure NMRNet service: explicit policy, allocation factory, announcement supplier and lifecycle/diagnostic callbacks are required."
            )
        if type(claims) is not AttemptClaims:
            raise ValueError("NMRNet service requires the shared Attempt claims.")
        self.config, self.api, self._claims = config, api, claims
        self._attempt_stopping = None
        self.delivery = DeliveryClient(config.delivery.endpoint, exchange_seconds=config.delivery.request_seconds,
                                       retirement_seconds=config.delivery.retirement_seconds)
        self._open_attempt, self._announcement, self._on_started = open_attempt, current_announcement, on_started
        self._on_api_retry, self._on_api_recovered = on_api_retry, on_api_recovered
        self._on_observed_crash = on_observed_crash
        self._factory_ready, self._on_source_exhausted = factory_ready, on_source_exhausted
        self.policy = policy
        self.hello_api = ProviderAPI(
            config.api.identity, config.api.http.new_transport(), expected_topology=config.api.expected_topology
        )
        self.controller = None
        self.work_task = None
        self._hello_task = None
        self._settlement_tasks = {}
        self._expected_cancellations = set()
        self._failures = []
        self.last_failure = None
        self.unconfirmed_hello = None
        self._state = _State.NEW
        self._stop = None

    def _owned_controllers(self):
        owners = dict.fromkeys((*self._claims.live_owners(), *self._settlement_tasks))
        if self.controller is not None:
            owners[self.controller] = None
        return owners

    @property
    def shutdown_ready(self):
        """Keep pending reports alive even after their workers and I/O stop."""
        return (
            self._state not in (_State.RUNNING, _State.SETTLING)
            and all(task is None or task.done() for task in (
                self.work_task, self._hello_task, *self._settlement_tasks.values(),
            ))
            and all(controller.shutdown_ready for controller in self._owned_controllers())
            and self.api.retirement_confirmed
            and self.hello_api.retirement_confirmed
            and self.delivery.retirement_confirmed
        )

    async def run(self, stop):
        """Run once; stop prohibits new allocation while admitted work drains."""
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
            if not stop.is_set() and await self._await_startup():
                self._hello_task = self._start_task(self._hello_loop)
                self.work_task = self._start_task(self._work_loop)
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

    async def _await_startup(self):
        # Startup may wait for publication availability. Keep its original task
        # in the same cancellable lane as ordinary work, not maintenance.
        self.work_task = self._start_task(self._startup)
        stopped = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait({self.work_task, stopped}, return_when=asyncio.FIRST_COMPLETED)
            return self.work_task.result() if self.work_task.done() else False
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)

    async def _startup(self):
        if self._stop.is_set():
            return False
        try:
            await self._read_until_available(self._admission)
        except DeliveryFull:
            pass  # Announce the service; the paced work loop waits for relay capacity.
        if self._stop.is_set():
            return False
        await self._hello_until_available()
        if self._stop.is_set():
            return False
        self._on_started()
        return not self._stop.is_set()

    async def _admission(self):
        return await require_admission_ready(self.config, self.api, delivery=self.delivery,
                                             policy=self.policy.admission, owner=self)

    async def _select_after_admission(self):
        """Observe the complete live view before starting a fresh discovery walk."""
        try:
            relay = await self._admission()
        except DeliveryFull:
            return None
        if self._stop.is_set():
            return None
        return await select_pending_start(self.config, self.api, policy=self.policy.admission, owner=self,
                                         excluded_jobs=frozenset(item["job_ref"] for item in relay["items"]), claims=self._claims)

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
        """Pace reviewed API availability and idle relay reads after I/O settles.

        The read caller repeats its whole view; Hello constructs current state.
        This owner never retains request bytes or authorizes another mutation.
        """
        delay, outage, outage_kind = self.policy.idle_seconds, None, kind
        while not self._stop.is_set():
            previous_failure = api.last_failure
            try:
                result = await operation()
            except asyncio.CancelledError as cause:
                if (
                    api is self.api and cause.__cause__ is None
                    and api.last_failure is not None and api.last_failure is not previous_failure
                ):
                    # Startup reads now run in a retained child task. Preserve
                    # its new interruption; Hello has its own uncertainty output.
                    raise cause from api.last_failure
                raise
            except (ApiFailure, DeliveryUnavailable) as failure:
                relay_read = isinstance(failure, DeliveryUnavailable)
                if relay_read and (api is not self.api or kind != "read"):
                    raise
                failure_kind = "delivery" if relay_read else kind
                try:
                    if relay_read:
                        await self.delivery.retire()
                    else:
                        await api.retire(owner=self)
                except (Exception, asyncio.CancelledError, BaseExceptionGroup):
                    self._remember(f"API {failure_kind} before failed transport retirement", failure)
                    raise
                if not (self.delivery.can_retry(failure) if relay_read else permit_retry(failure)):
                    raise
                if self._stop.is_set():
                    return None
                outage, outage_kind = failure, failure_kind
                wait = random.uniform(delay / 2, delay)
                if not relay_read:
                    wait += failure.retry_after_seconds or 0
                self._on_api_retry(failure_kind, failure, wait)
                if await self._pause(wait):
                    return None
                delay = min(self.policy.api_backoff_max_seconds, delay * 2)
            else:
                if outage is not None:
                    self._on_api_recovered(outage_kind, outage)
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
            self._attempt_stopping = asyncio.Event()
            if self._stop.is_set():
                self._attempt_stopping.set()
                return
            self.controller = await self._open_attempt(pending, stopping=self._attempt_stopping)
            # Adoption precedes this stop check, including a late factory return.
            # Shutdown observes work_task before settling that exact controller.
            if self._stop.is_set():
                return
            controller = self.controller
            try:
                await self._observe_attempt(controller, pending)
            except ControllerFailed as failure:
                execution = failure.cause
                if (type(execution) is ExecutionFailed and type(execution.cause) is UploadRecoveryExhausted
                        and not failure.cleanup_failures and not execution.cleanup_failures
                        and not execution.cause.cleanup_failures and execution.cause.interruption is None
                        and execution.api_failure is None and execution.condition_failure is None
                        and execution.latest_condition_failure is None and execution.delivery_failure is None
                        and execution.operation_interruption is None and execution.interruption_cause is None
                        and execution.handoff_interruption is None
                        and controller.work_settled and controller.shutdown_ready and self._factory_ready()
                        and controller in self._claims.live_owners()
                        and self._claims.owns_job(pending.scope, pending.input.job_ref)):
                    self._on_source_exhausted(failure, pending)
                    # The registry keeps this controller and the Job exclusion.
                    self.controller = None
                    continue
                if type(failure.cause) is not LiveCapacityFull:
                    raise
                # Capacity refusal precedes START. Retire this unused session;
                # independent cleanup failures still prevent another selection.
                await controller.settle()
                LOGGER.info("Live Attempt bookkeeping is full; no START was sent. Waiting for capacity.")
                self.controller = None
                await self._pause(self.policy.idle_seconds)
                continue
            # Held work remains reachable through the shared live registry.
            self.controller = None

    async def _observe_attempt(self, controller, pending):
        def report_crash():
            operation = controller.operation
            if isinstance(operation, AttemptExecutor) and operation.observed_crash is not None:
                self._on_observed_crash(operation.observed_crash)

        async def run():
            try:
                result = await controller.run(pending)
            except BaseException as primary:
                try:
                    report_crash()
                except (Exception, BaseExceptionGroup, SystemExit, KeyboardInterrupt) as notice:
                    raise BaseExceptionGroup(
                        "Attempt failed and its crash diagnostic could not be reported.", (primary, notice),
                    ) from None
                raise
            try:
                report_crash()
            except Exception as notice:
                self._remember("native crash diagnostic", notice)
            return result

        task = self._start_task(run)
        interruption = None
        cancellations = asyncio.current_task().cancelling()
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError as cause:
                if task.done() and task.cancelled():
                    try:
                        task.result()
                    except asyncio.CancelledError as child:
                        observer = interruption
                        if observer is None and asyncio.current_task().cancelling() > cancellations:
                            observer = cause
                        if observer is not None:
                            raise BaseExceptionGroup(
                                "Attempt observation and the original Attempt were both cancelled.",
                                (observer, child),
                            ) from None
                        raise
                if interruption is None:
                    interruption = cause
                    self._stop.set()
            except (Exception, BaseExceptionGroup) as cause:
                if interruption is not None:
                    raise BaseExceptionGroup(
                        "Attempt observation was cancelled and the original Attempt also failed.",
                        (interruption, cause),
                    ) from None
                raise
        if interruption is not None:
            raise interruption
        return result

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
        task = asyncio.create_task(contain_task_exit(operation))
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
        if self.work_task is None or self.work_task.done():
            for operation, function in (
                ("Attempt settlement", lambda: self._settle_controller(retry=retry_settlement)),
                ("execution API retirement", self._retire_api),
                ("delivery control retirement", self.delivery.retire),
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

    async def _finish_work(self, drain):
        try:
            if drain:
                finished = await self._wait_task(self.work_task, self.policy.drain_seconds)
                if not finished and self.controller is not None:
                    try:
                        LOGGER.warning("Provider shutdown is waiting for the original Attempt operation and any result handoff.")
                    except Exception as cause:
                        self._remember("shutdown notice", cause)
                    await asyncio.wait({self.work_task})
        finally:
            # Revoke remaining effects after work finishes or observation is interrupted.
            if self._attempt_stopping is not None:
                self._attempt_stopping.set()
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
        # These are actual settlement tasks, not copies of controller phases.
        failures = []
        for controller in self._owned_controllers():
            previous = self._settlement_tasks.get(controller)
            if previous is None and controller.shutdown_ready:
                # A passive live exclusion can be quiescent without permission
                # to release its claim. Keep the registry's original owner.
                if self.controller is controller:
                    self.controller = None
                continue
            if previous is None or (retry and previous.done() and (previous.cancelled() or previous.exception() is not None)):
                previous = self._start_task(controller.settle)
                self._settlement_tasks[controller] = previous
            try:
                if not await self._wait_task(previous, self.policy.settle_seconds):
                    raise ServiceRefused(
                        "Original Attempt settlement is still pending; retain its controller and task before releasing dependencies."
                    )
                previous.result()
            except (Exception, asyncio.CancelledError, BaseExceptionGroup) as cause:
                failures.append(cause)
            else:
                del self._settlement_tasks[controller]
                if self.controller is controller:
                    self.controller = None
        if failures:
            raise BaseExceptionGroup("NMRNet could not settle every live Attempt", failures)

    async def _retire_api(self):
        if self.api.pending_owner is not None and self.api.pending_owner is not self:
            raise ServiceRefused(
                "Execution API still belongs to another operation; settle its original owner before releasing dependencies."
            )
        await self.api.retire(owner=self)

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
