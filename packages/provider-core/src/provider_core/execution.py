"""Order one prepared execution and retain its outcome before publication.

NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_lifecycle.py::execute_prepared confirms running progress,
records execution entry and observes current state before invoking its live handle.
Readback is a snapshot, never an atomic grant against later server cancellation.
Its lines 1287-1340 also poll generation and observe before accepting candidates;
this executor extends that observation to asynchronous preparation as well.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import math
from enum import Enum

from provider_core.analysis import AnalysisFailure, AnalysisSuccess
from provider_core.api import ApiFailure
from provider_core.attempt_record import (
    AcknowledgedAttempt,
    LocalExecutionPhase,
    SuppressedOutcome,
    SuppressionReason,
    TerminalOperation,
    TerminalPending,
    mark_execution_entered,
)
from provider_core.attempt_request import complete_body, fail_body
from provider_core.attempt_store import StoreFailed, StoreRefused


class _Retirement(Enum):
    NOT_ATTEMPTED = "not_attempted"
    UNCONFIRMED = "unconfirmed"
    CONFIRMED = "confirmed"


class ExecutionRefused(RuntimeError):
    """No scientific execution was invoked by this refusal."""


class ExecutionStopped(RuntimeError):
    def __init__(self, observation):
        super().__init__(
            f"Cannot proceed with analysis: observed Attempt {observation.state} and Job {observation.job_state}; preserve the current record"
        )
        self.observation = observation


class ExecutionFailed(RuntimeError):
    """Safe context with protected primary, outcome and later cleanup evidence."""

    def __init__(self, *, cause, outcome, unconfirmed_record, api_failure, cleanup_failures):
        super().__init__(
            "Analysis did not complete its owned lifecycle; preserve the observed outcome and settle original work before reuse"
        )
        self.cause = cause
        self.outcome = outcome
        self.unconfirmed_record = unconfirmed_record
        self.api_failure = api_failure
        self.cleanup_failures = cleanup_failures


def _suppression_reason(cause):
    if isinstance(cause, ExecutionStopped):
        return SuppressionReason.SERVER_STOP
    if isinstance(cause, ApiFailure):
        return SuppressionReason.OBSERVATION_FAILED
    if isinstance(cause, asyncio.CancelledError):
        return SuppressionReason.CALLER_CANCELLED
    return None


def _observe_task_failure(task):
    # The retained task still owns its result. Retrieve exceptions to prevent
    # abandoned tasks from writing private binding failures to the event-loop log.
    if not task.cancelled():
        task.exception()


class AttemptExecutor:
    """Own one run; settlement never resumes preparation or scientific execution.

    Session ownership transfers after local preconditions and claim acquisition.
    A precondition refusal leaves the supplied session with its caller. After that
    point all failure paths attempt retirement, retaining uncertainty on failure.
    """

    def __init__(self, api, store, claim, session, *, poll_seconds, task_settle_seconds):
        if api.origin != store.scope.api_origin:
            raise ValueError("Cannot configure execution: signing origin differs from retained storage scope")
        for name, seconds in (("poll_seconds", poll_seconds), ("task_settle_seconds", task_settle_seconds)):
            if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(f"Cannot configure execution: {name} must be finite and positive")
        self.api, self.store, self.claim, self.session = api, store, claim, session
        self._poll_seconds = poll_seconds
        self._task_settle_seconds = task_settle_seconds
        self._task = None
        self._cancel_requested = False
        self._started = False
        self._active = False
        self._observing = False
        self._record = None
        self._unconfirmed = None
        self._outcome = None
        self._api_failure = None
        self._retirement = _Retirement.NOT_ATTEMPTED
        self.last_failure = None

    async def run(self, acknowledged):
        if self._started:
            raise ExecutionRefused("This executor already accepted a run; settlement cannot authorize another execution")
        if (
            type(acknowledged) is not AcknowledgedAttempt
            or acknowledged.local_phase is not LocalExecutionPhase.NOT_ENTERED
            or acknowledged.admission.state != "in_progress"
            or acknowledged.start.scope != self.store.scope
        ):
            raise ExecutionRefused("Execution requires a matching active acknowledgement that has never entered science")
        self.claim.require_start(acknowledged.start)
        self.claim.require(self.store.scope, acknowledged.admission.execution_attempt_ref)
        self.claim.begin(self)
        self._started = self._active = self._observing = True
        self._record = acknowledged
        try:
            if acknowledged not in await self.store.records(owner=self):
                raise ExecutionRefused("Exact acknowledgement is not current in storage; reread it before preparing work")
            await self._observe_active()
            instructions = await self._api(
                self.api.job_input, acknowledged.start.input, analysis_kind_ref=acknowledged.start.analysis_kind_ref
            )
            await self._progress("preparing")
            prepared = await self._monitor("prepare", acknowledged, instructions)
            if type(prepared) is AnalysisFailure:
                self._outcome = prepared
            else:
                await self._progress("running")
                await self._write(mark_execution_entered(acknowledged))
                await self._observe_active()
                await self._monitor("execute", prepared)
            await self._write(self._terminal())
            await self._retire_session()
            return self._record
        except asyncio.CancelledError as cause:
            self._remember(cause)
            await self._retire_after_failure()
            raise
        except Exception as cause:
            # This is the execution boundary: retain even unexpected binding or
            # codec failures, stop owned work, and never classify them as bad input.
            self._remember(cause)
            await self._retire_after_failure()
            raise self.last_failure from None
        finally:
            self._observing = False
            self._refresh_failure()
            self._finish_if_settled()

    async def _call_session(self, phase, *args):
        result = await getattr(self.session, phase)(*args)
        if phase == "execute" or type(result) is AnalysisFailure:
            # Capture before any final read or cancellation can compete with the
            # completed result. A stop never grants permission to publish it.
            self._outcome = result
            self._refresh_failure()
        return result

    async def _monitor(self, phase, *args):
        self._cancel_requested = False
        self._task = asyncio.create_task(self._call_session(phase, *args))
        self._task.add_done_callback(_observe_task_failure)
        while not self._task.done():
            await asyncio.wait((self._task,), timeout=self._poll_seconds)
            if not self._task.done():
                await self._observe_active()
        task = self._task
        self._task = None
        result = task.result()
        # A known binding failure needs no acceptance read. For a successful
        # return, finish observation before accepting the handle or outcome.
        await self._observe_active()
        return result

    def _cancel_task(self):
        if self._task is not None and not self._task.done() and not self._cancel_requested:
            self._cancel_requested = True
            self._task.cancel()

    async def _settle_task(self):
        if self._task is None:
            return
        await asyncio.wait((self._task,), timeout=self._task_settle_seconds)
        if not self._task.done():
            raise TimeoutError("Analysis task has not stopped; retain ownership and settle the original task")
        task = self._task
        self._task = None
        if not task.cancelled():
            task.result()

    async def _api(self, operation, *args, **kwargs):
        try:
            return await operation(*args, **kwargs, owner=self)
        except asyncio.CancelledError:
            self._api_failure = self.api.last_failure
            raise
        except ApiFailure as cause:
            self._api_failure = cause
            raise

    async def _observe_active(self):
        observed = await self._api(
            self.api.read_attempt, self._record.admission.execution_attempt_ref, job_ref=self._record.start.input.job_ref
        )
        if observed.state != "in_progress" or observed.job_state != "open":
            raise ExecutionStopped(observed)

    async def _progress(self, phase):
        await self._api(self.api.progress_attempt, self._record.admission.execution_attempt_ref, phase=phase, condition_code=None)

    async def _write(self, record):
        self._unconfirmed = record
        try:
            self._record = await self.store.replace(self._record, record, owner=self)
        except StoreRefused:
            # Refusal establishes no replacement effect; an observed scientific
            # outcome remains a separate retention obligation.
            self._unconfirmed = None
            raise
        self._unconfirmed = None

    def _terminal(self):
        reference = self._record.admission.execution_attempt_ref
        if type(self._outcome) is AnalysisSuccess:
            body = complete_body(reference, self._outcome.result_schema_id, self._outcome.content)
            operation = TerminalOperation.COMPLETE
        elif type(self._outcome) is AnalysisFailure:
            body = fail_body(reference, self._outcome.code, self._outcome.message)
            operation = TerminalOperation.FAIL
        else:
            raise TypeError("Analysis session returned an unsupported outcome; preserve it for binding diagnosis")
        return TerminalPending(self._record, operation, body, self._outcome.evidence)

    async def _retire_session(self):
        if self._retirement is not _Retirement.CONFIRMED:
            self._retirement = _Retirement.UNCONFIRMED
            result = await self.session.retire()
            if result is not None:
                raise TypeError("Session retirement must return None after confirmed cleanup, otherwise raise")
            self._retirement = _Retirement.CONFIRMED

    async def _retire_after_failure(self):
        self._cancel_task()
        operations = [self._settle_task]
        if self._retirement is _Retirement.NOT_ATTEMPTED:
            # Do not retry failed normal retirement while handling its own error.
            operations.insert(0, self._retire_session)
        cancellation = None
        for operation in operations:
            try:
                await operation()
            except asyncio.CancelledError as cause:
                self._remember(cause, cleanup=True)
                cancellation = cause
            except Exception as cause:
                self._remember(cause, cleanup=True)
        if cancellation is not None:
            raise cancellation from None

    async def _retire_api(self):
        if self.api.pending_owner is self:
            await self.api.retire(owner=self)

    async def _settle_storage(self):
        if self.store.pending_owner is self:
            try:
                settled = await self.store.settle(owner=self)
            except StoreRefused:
                if self.store.pending_owner is not self:
                    self._unconfirmed = None
                raise
            if self._unconfirmed is not None:
                if settled.value != self._unconfirmed:
                    raise StoreFailed("Settled write differs from the executor's retained intent; reconcile storage")
                self._record = settled.value
                self._unconfirmed = None

    async def settle(self):
        """Retire work and observe original I/O; never invoke a prepared handle."""
        if not self._active or self._observing:
            raise ExecutionRefused("No unobserved execution lifecycle is available to settle")
        self._observing = True
        cancellation = None
        failed = False
        try:
            self._cancel_task()
            # Independent cleanup must still run when another owner fails to
            # retire. Each port supplies its own bounded observation contract.
            for operation in (self._retire_session, self._settle_task, self._retire_api, self._settle_storage):
                try:
                    await operation()
                except asyncio.CancelledError as cause:
                    cancellation = cause
                    failed = True
                    self._remember(cause, cleanup=True)
                except Exception as cause:
                    failed = True
                    self._remember(cause, cleanup=True)
            if cancellation is not None:
                raise cancellation from None
            if self._unconfirmed is not None or self._outcome_unretained():
                failed = True
                message = "Observed outcome or record write requires explicit reconciliation; the claim remains held"
                if (self._unconfirmed is None and type(self._outcome) in (AnalysisSuccess, AnalysisFailure)
                        and _suppression_reason(self.last_failure.cause) is not None):
                    message = "Returned analysis evidence is not durable; settle remaining tasks and I/O before calling retain_suppressed_outcome() to preserve it without publication; the claim remains held"
                self._remember(
                    StoreFailed(message),
                    cleanup=True,
                )
            if failed:
                raise self.last_failure from None
            return self._record if type(self._record) is TerminalPending else None
        finally:
            self._observing = False
            self._refresh_failure()
            self._finish_if_settled()

    async def retain_suppressed_outcome(self):
        """Explicitly persist returned evidence without selecting it for publication.

        Settle original tasks and I/O first. This method starts one owned CAS write;
        cancellation retains that write for settle(), never a replacement attempt.
        Persistence alone cannot release unconfirmed worker retirement.
        """
        if (not self._active or self._observing or self.last_failure is None
                or type(self._outcome) not in (AnalysisSuccess, AnalysisFailure)
                or type(self._record) is not AcknowledgedAttempt):
            raise ExecutionRefused("Cannot retain a suppressed outcome: this interrupted executor has no unretained admitted result")
        if (self._task is not None or self._unconfirmed is not None
                or self.api.pending_owner is self or self.store.pending_owner is self):
            raise ExecutionRefused("Cannot retain a suppressed outcome while original work or I/O is unresolved; settle it first")
        reason = _suppression_reason(self.last_failure.cause)
        if reason is None:
            raise ExecutionRefused("Cannot suppress this outcome: its failure was not a server stop, failed observation or caller cancellation")
        self._observing = True
        try:
            await self._write(SuppressedOutcome(self._terminal(), reason))
            return self._record
        except asyncio.CancelledError as cause:
            self._remember(cause, cleanup=True)
            raise
        except Exception as cause:
            self._remember(cause, cleanup=True)
            raise self.last_failure from None
        finally:
            self._observing = False
            self._refresh_failure()
            self._finish_if_settled()

    def _outcome_unretained(self):
        return self._outcome is not None and type(self._record) not in (TerminalPending, SuppressedOutcome)

    def _finish_if_settled(self):
        if (
            self._retirement is _Retirement.CONFIRMED
            and self._task is None
            and self.api.pending_owner is not self
            and self.store.pending_owner is not self
            and self._unconfirmed is None
            and not self._outcome_unretained()
        ):
            self.claim.finish(self)
            self._active = False

    def _refresh_failure(self):
        if self.last_failure is not None:
            self.last_failure.outcome = self._outcome
            self.last_failure.unconfirmed_record = self._unconfirmed

    def _remember(self, cause, *, cleanup=False):
        original = self.last_failure if cleanup else None
        self.last_failure = ExecutionFailed(
            cause=original.cause if original is not None else cause,
            outcome=self._outcome,
            unconfirmed_record=self._unconfirmed,
            api_failure=self._api_failure,
            cleanup_failures=(*original.cleanup_failures, cause) if original is not None else ((cause,) if cleanup else ()),
        )
