"""Own one selected session and its claim across the existing lifecycle owners.

The sequence already exercised by NMRNet's tests/integration/provider_core/
test_live_worker.py::LiveWorkerTests.run_lifecycle becomes runtime composition
here. Scientific preparation, durable effects and retirement retain
their existing owners. This is neither a scheduler nor an automatic recovery loop.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
from enum import Enum, auto

from provider_core.analysis import retire_session
from provider_core.attempt_record import TerminalConfirmed
from provider_core.execution import AttemptExecutor, require_execution_settings, require_observation_recovery
from provider_core.publication import TerminalPublisher, TerminalRefusalPolicy, require_refusal_policy
from provider_core.starting import AttemptStarter


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

    # silent-defaults: allow-contract -- Consumers select terminal refusal and observation recovery before session handoff; defaults retain pending commands and fail-stop reads, without granting automatic replay.
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
    ):
        if api.origin != store.scope.api_origin:
            raise ValueError("Cannot configure Attempt controller: signing origin differs from retained storage scope")
        require_execution_settings(
            allowed_job_states=allowed_job_states, poll_seconds=poll_seconds, task_settle_seconds=task_settle_seconds
        )
        require_refusal_policy(refusal_policy)
        require_observation_recovery(observation_recovery)
        self._refusal_policy = refusal_policy
        self._observation_recovery = observation_recovery
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
        acknowledged = await self._operation.start(pending)
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
        )
        terminal = await self._operation.run(acknowledged)
        # The executor confirmed session retirement before returning this record.
        self._operation = TerminalPublisher(self.api, self.store, self._claim, refusal_policy=self._refusal_policy)
        return await self._operation.publish(terminal)

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
