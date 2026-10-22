"""Own one selected session and its claim across the existing lifecycle owners.

The sequence already exercised by NMRNet's tests/integration/provider_core/
test_live_worker.py::LiveWorkerTests.run_lifecycle becomes runtime composition
here. Scientific preparation, durable effects and retirement retain
their existing owners. Optional START/terminal recovery repeats only the exact
durable command, before science or after its retirement; this owner never reruns
a session.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto

from provider_core.analysis import retire_session
from provider_core.api import ApiFailure
from provider_core.attempt_record import AcknowledgedAttempt, StartPending, TerminalConfirmed
from provider_core.execution import (
    AttemptExecutor,
    require_condition_recovery,
    require_execution_settings,
    require_observation_recovery,
    require_phase_progress_observer,
)
from provider_core.publication import PublicationFailed, TerminalPublisher, TerminalRefusalPolicy, require_refusal_policy
from provider_core.starting import AttemptStarter, StartFailed


class ControllerRefused(RuntimeError):
    """This invocation dispatched no new start or scientific work."""


class ControllerFailed(RuntimeError):
    """Safe lifecycle context with protected primary and independent cleanup causes."""

    def __init__(self, cause, cleanup_failures):
        super().__init__(
            "The Attempt lifecycle did not finish; retain this controller and its records, then settle original work before recovery."
        )
        self.cause = cause
        self.cleanup_failures = cleanup_failures


def require_terminal_window(seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Terminal recovery window_seconds must be finite and positive")


def require_start_window(seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("START recovery window_seconds must be finite and positive")


@dataclass(frozen=True, slots=True)
class StartRecovery:
    """Bound exact START retry admission before scientific preparation.

    The window begins at the first failed send and includes retirement/backoff.
    Already admitted bounded I/O may finish later. Protected synchronous observers
    receive the original pending Job before retry and its durable admission after
    recovery; no Attempt reference is assumed before a verified acknowledgement.
    """

    window_seconds: float
    backoff_max_seconds: float
    on_retry: Callable[[StartPending, ApiFailure, float], None] = field(repr=False)
    on_recovered: Callable[[AcknowledgedAttempt, ApiFailure], None] = field(repr=False)

    def __post_init__(self):
        require_start_window(self.window_seconds)
        value = self.backoff_max_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("START recovery backoff maximum must be finite positive seconds")
        if not callable(self.on_retry) or not callable(self.on_recovered):
            raise ValueError("START recovery requires explicit protected retry and recovery observers")


class StartRecoveryExpired(TimeoutError):
    """Retry admission ended; retain the same pending START and original send."""

    def __init__(self, window_seconds, *, cause, pending):
        super().__init__(
            f"START retry window of {window_seconds:g} seconds expired; "
            "retain the exact request and settle original I/O before recovery"
        )
        self.window_seconds, self.cause, self.pending = window_seconds, cause, pending


@dataclass(frozen=True, slots=True)
class TerminalRecovery:
    """Bound admission of exact publication retries, with protected observers.

    The window begins at the first send failure, including retirement/backoff.
    No publisher invocation begins after expiry; an admitted invocation and its
    original bounded I/O/retirement may finish later. Callbacks are bounded,
    synchronous and receive the actual acknowledgement and failed send.
    """

    window_seconds: float
    backoff_max_seconds: float
    on_retry: Callable[[AcknowledgedAttempt, ApiFailure, float], None] = field(repr=False)
    on_recovered: Callable[[AcknowledgedAttempt, ApiFailure], None] = field(repr=False)

    def __post_init__(self):
        require_terminal_window(self.window_seconds)
        value = self.backoff_max_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Terminal recovery backoff maximum must be finite positive seconds")
        if not callable(self.on_retry) or not callable(self.on_recovered):
            raise ValueError("Terminal recovery requires explicit protected retry and recovery observers")


class TerminalRecoveryExpired(TimeoutError):
    """Retry admission ended; the last send and its durable obligation remain owned."""

    def __init__(self, window_seconds, *, cause, attempt):
        super().__init__(
            f"Terminal publication retry window of {window_seconds:g} seconds expired; "
            "retain the exact command and settle original I/O before recovery"
        )
        self.window_seconds, self.cause, self.attempt = window_seconds, cause, attempt


class _Lifetime(Enum):
    NEW = auto()
    RUNNING = auto()
    HELD = auto()
    SETTLING = auto()
    RELEASED = auto()


class AttemptController:
    """Accept one session after configuration validation, including pre-start cleanup.

    Composition supplies the shared claim registry and already opened scoped
    store, and retains API/store lifetime. The accepted session belongs here even
    when a start fails or returns a terminal replay. Constructor refusal leaves
    it with the caller. A held controller never starts or publishes fresh work.

    `operation` retains the actual core owner for protected diagnosis and explicit
    executor outcome retention. Settlement observes outstanding work and requests
    session retirement when needed. It cannot start scientific execution after a
    late admission or publish a late scientific outcome.
    """

    # silent-defaults: allow-contract -- Consumers select recovery and unconfirmed-phase handling before session handoff; defaults keep fail-stop reads, progress and mutations.
    def __init__(
        self,
        api,
        store,
        claims,
        session,
        *,
        allowed_job_states,
        poll_seconds,
        task_settle_seconds,
        refusal_policy=TerminalRefusalPolicy.RETAIN_PENDING,
        observation_recovery=None,
        on_unconfirmed_phase_progress=None,
        condition_recovery=None,
        start_recovery=None,
        terminal_recovery=None,
    ):
        if api.origin != store.scope.api_origin:
            raise ValueError("Cannot configure Attempt controller: signing origin differs from retained storage scope")
        require_execution_settings(
            allowed_job_states=allowed_job_states, poll_seconds=poll_seconds, task_settle_seconds=task_settle_seconds
        )
        require_refusal_policy(refusal_policy)
        require_observation_recovery(observation_recovery)
        require_phase_progress_observer(on_unconfirmed_phase_progress)
        require_condition_recovery(condition_recovery)
        if start_recovery is not None and type(start_recovery) is not StartRecovery:
            raise ValueError("START recovery must be an admitted policy or explicit fail-stop None")
        if terminal_recovery is not None and type(terminal_recovery) is not TerminalRecovery:
            raise ValueError("Terminal recovery must be an admitted policy or explicit fail-stop None")
        self._refusal_policy = refusal_policy
        self._observation_recovery = observation_recovery
        self._on_unconfirmed_phase_progress = on_unconfirmed_phase_progress
        self._condition_recovery = condition_recovery
        self._start_recovery = start_recovery
        self._terminal_recovery = terminal_recovery
        self.api, self.store, self._claims, self.session = api, store, claims, session
        self._allowed_job_states = allowed_job_states
        self._poll_seconds, self._task_settle_seconds = poll_seconds, task_settle_seconds
        self._lifetime = _Lifetime.NEW
        self._claim = None
        self._operation = None
        self._retired_by_controller = False
        self.last_failure = None

    @property
    def operation(self):
        """Return the retained start, execution, publication or session owner.

        The object remains available for diagnosis after settlement; its presence
        does not mean work is still running or that the claim is still borrowed.
        """
        return self._operation

    async def run(self, pending):
        if self._lifetime is not _Lifetime.NEW:
            raise ControllerRefused(
                "This controller accepts only one run; retain its original invocation and controller for settlement or recovery"
            )
        self._lifetime = _Lifetime.RUNNING
        try:
            result = await self._run_effects(pending)
            self._claim.release()
            self._lifetime = _Lifetime.RELEASED
            return result
        except asyncio.CancelledError as cause:
            self._remember(cause)
            await self._retire_after_failure()
            raise
        except Exception as cause:
            self._remember(cause)
            await self._retire_after_failure()
            raise self.last_failure from None
        finally:
            if self._lifetime is _Lifetime.RUNNING:
                self._lifetime = _Lifetime.HELD

    async def _run_effects(self, pending):
        inventory = await self.store.records(owner=self)
        if any(type(record) is not TerminalConfirmed for record in inventory):
            raise ControllerRefused("Retained work requires explicit recovery before this session can start a fresh Attempt")
        self._claim = self._claims.claim_start(pending)
        self._operation = AttemptStarter(self.api, self.store, self._claim)
        acknowledged = await self._start_attempt(pending)
        if acknowledged.admission.state != "in_progress":
            self._operation = self.session
            await self._retire_session()
            return acknowledged
        self._operation = AttemptExecutor(
            self.api,
            self.store,
            self._claim,
            self.session,
            allowed_job_states=self._allowed_job_states,
            poll_seconds=self._poll_seconds,
            task_settle_seconds=self._task_settle_seconds,
            observation_recovery=self._observation_recovery,
            on_unconfirmed_phase_progress=self._on_unconfirmed_phase_progress,
            condition_recovery=self._condition_recovery,
        )
        terminal = await self._operation.run(acknowledged)
        # The executor confirmed session retirement before returning this record.
        self._operation = TerminalPublisher(self.api, self.store, self._claim, refusal_policy=self._refusal_policy)
        return await self._publish_terminal(terminal)

    async def _start_attempt(self, pending):
        policy = self._start_recovery
        deadline, first_failure = None, None
        delay = min(self._poll_seconds, policy.backoff_max_seconds) if policy is not None else None
        send = self._operation.start
        while True:
            try:
                acknowledged = await send(pending)
            except StartFailed as failure:
                if (
                    type(failure) is not StartFailed
                    or policy is None
                    or failure.cause is not failure.api_failure
                    or failure.api_failure is None
                    or failure.acknowledgement is not None
                    or failure.settlement_failures
                ):
                    raise
                if deadline is None:
                    deadline = asyncio.get_running_loop().time() + policy.window_seconds
                # Settlement belongs to this same starter. Late transport or
                # independent cleanup evidence must be admitted before replay.
                if self._claim.operation_owner is self._operation:
                    await self._operation.settle()
                if not self.api.can_replay_start(failure.api_failure):
                    raise
                if first_failure is None:
                    first_failure = failure.api_failure
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise StartRecoveryExpired(policy.window_seconds, cause=failure, pending=pending) from None
                seconds = min(remaining, random.uniform(delay / 2, delay))
                policy.on_retry(pending, failure.api_failure, seconds)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise StartRecoveryExpired(policy.window_seconds, cause=failure, pending=pending) from None
                await asyncio.sleep(min(seconds, remaining))
                if asyncio.get_running_loop().time() >= deadline:
                    raise StartRecoveryExpired(policy.window_seconds, cause=failure, pending=pending) from None
                delay = min(policy.backoff_max_seconds, delay * 2)
                send = self._operation.resume
            else:
                if first_failure is not None:
                    policy.on_recovered(acknowledged, first_failure)
                return acknowledged

    async def _publish_terminal(self, terminal):
        policy = self._terminal_recovery
        deadline, first_failure = None, None
        delay = min(self._poll_seconds, policy.backoff_max_seconds) if policy is not None else None
        while True:
            try:
                result = await self._operation.publish(terminal)
            except PublicationFailed as failure:
                if (
                    type(failure) is not PublicationFailed
                    or policy is None
                    or failure.cause is not failure.api_failure
                    or failure.api_failure is None
                    or failure.outcome is not None
                    or failure.settlement_failures
                ):
                    raise
                if deadline is None:
                    deadline = asyncio.get_running_loop().time() + policy.window_seconds
                # Only the original publisher may settle its borrowed I/O.
                # A cleanup failure propagates that owner's updated evidence,
                # even when the retry window also expires during retirement.
                if self._claim.operation_owner is self._operation:
                    await self._operation.settle()
                if not self.api.can_replay_terminal(failure.api_failure):
                    raise
                if first_failure is None:
                    first_failure = failure.api_failure
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TerminalRecoveryExpired(policy.window_seconds, cause=failure, attempt=terminal.attempt) from None
                seconds = min(remaining, random.uniform(delay / 2, delay))
                policy.on_retry(terminal.attempt, failure.api_failure, seconds)
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TerminalRecoveryExpired(policy.window_seconds, cause=failure, attempt=terminal.attempt) from None
                await asyncio.sleep(min(seconds, remaining))
                if asyncio.get_running_loop().time() >= deadline:
                    raise TerminalRecoveryExpired(policy.window_seconds, cause=failure, attempt=terminal.attempt) from None
                delay = min(policy.backoff_max_seconds, delay * 2)
            else:
                if first_failure is not None and type(result) is TerminalConfirmed:
                    policy.on_recovered(terminal.attempt, first_failure)
                return result

    async def _retire_after_failure(self):
        # silent-defaults: allow-contract -- Admission can fail before a claim exists; that state has no borrowed operation to settle.
        owner = self._claim.operation_owner if self._claim is not None else None
        if isinstance(owner, AttemptExecutor) or self._operation is self.session:
            return
        try:
            await self._retire_session()
        except asyncio.CancelledError as cause:
            self._remember(cause, cleanup=True)
            raise
        except Exception as cause:
            self._remember(cause, cleanup=True)

    async def settle(self):
        """Settle exact owners and release only after confirmed session retirement."""
        if self._lifetime in (_Lifetime.RUNNING, _Lifetime.SETTLING):
            raise ControllerRefused("The controller is already observing its lifetime; await that invocation before settlement")
        if self._lifetime is _Lifetime.RELEASED:
            return None
        self._lifetime = _Lifetime.SETTLING
        result = None
        failures = []
        try:
            # silent-defaults: allow-contract -- A refused admission has no claim; session and inventory cleanup still run without inventing an operation owner.
            owner = self._claim.operation_owner if self._claim is not None else None
            if isinstance(owner, AttemptExecutor):
                operations = (owner.settle,)
            else:
                operations = (self._retire_session, self._settle_operation, self._settle_inventory)
            for operation in operations:
                try:
                    value = await operation()
                    if value is not None:
                        result = value
                except (Exception, asyncio.CancelledError) as cause:
                    failures.append(cause)
                    self._remember(cause, cleanup=True)
            if failures:
                for cause in failures:
                    if isinstance(cause, asyncio.CancelledError):
                        raise cause
                raise self.last_failure from None
            if self._claim is not None:
                self._claim.release()
            self._lifetime = _Lifetime.RELEASED
            return result
        finally:
            if self._lifetime is _Lifetime.SETTLING:
                self._lifetime = _Lifetime.HELD

    async def _retire_session(self):
        if self._retired_by_controller or isinstance(self._operation, TerminalPublisher):
            return
        if isinstance(self._operation, AttemptExecutor) and self._operation.retirement_confirmed:
            return
        await retire_session(self.session)
        # Only this owner's pre-execution cleanup is recorded here. Execution's
        # confirmation stays with its executor; publication can only follow it.
        self._retired_by_controller = True

    async def _settle_operation(self):
        # silent-defaults: allow-contract -- Pre-claim admission failures leave no borrowed operation; their inventory and session settle through separate owners.
        owner = self._claim.operation_owner if self._claim is not None else None
        if owner is not None:
            return await owner.settle()
        return None

    async def _settle_inventory(self):
        if self.store.pending_owner is self:
            await self.store.settle(owner=self)

    # silent-defaults: allow-contract -- Ordinary call failures are primary; cleanup callers explicitly add an independent failure while preserving the primary cause.
    def _remember(self, cause, *, cleanup=False):
        original = self.last_failure
        cleanup_failures = ()
        if original is not None and cleanup:
            cleanup_failures = (*original.cleanup_failures, cause)
        self.last_failure = ControllerFailed(
            original.cause if original is not None else cause,
            cleanup_failures,
        )
