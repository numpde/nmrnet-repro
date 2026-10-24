"""Recover retained requests and observations without reconstructing scientific work.

NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_journal.py:534-579 distinguishes pending-start replay,
identity-bound observation and terminal replay. Unlike its full consumer policy,
core never resumes preparation, synthesizes interrupted failures or deletes records.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
from dataclasses import dataclass

from provider_core.api import ApiFailure
from provider_core.attempt_record import (
    AcknowledgedAttempt,
    StartPending,
    TerminalPending,
    TerminalReadNotVisible,
    TerminalReadRequired,
    TerminalStateObserved,
    acknowledged_attempt,
    pending_start,
)
from provider_core.attempt_response import AttemptObservation
from provider_core.attempt_store import StoreFailed, StoreRefused
from provider_core.publication import TerminalPublisher, TerminalRecovery, TerminalRefusalPolicy, require_refusal_policy
from provider_core.starting import AttemptStarter
from provider_core.transport import ExchangeFailed


class RecoveryRefused(RuntimeError):
    """The requested recovery action was not dispatched."""


class RecoveryFailed(RuntimeError):
    """Safe recovery context retaining protected original and cleanup failures."""

    # silent-defaults: allow-contract -- A read may fail before producing an observation or cleanup failure; absent evidence neither confirms a read nor settles ownership.
    def __init__(self, cause, cleanup_failures=(), *, api_failure=None, observed_record=None):
        super().__init__(
            "Cannot finish retained Attempt recovery; preserve its record and protected evidence, then settle I/O or reconcile storage before continuing"
        )
        self.cause = cause
        self.cleanup_failures = cleanup_failures
        self.api_failure = api_failure
        self.observed_record = observed_record


@dataclass(frozen=True, slots=True)
class ObservedAttempt:
    """Historical local record and current readback; never execution permission."""

    record: AcknowledgedAttempt
    observation: AttemptObservation


class AttemptRecovery:
    """One selected record, retaining the caller's claim after every disposition.

    Composition reserves its complete selected inventory through claim_records
    before replay and excludes the previous controller's mutation authority.
    Recovery does not prove worker exit or release resources for another analysis.
    """

    # silent-defaults: allow-contract -- Stopped recovery sends once unless its caller explicitly supplies outage pacing; terminal refusals retain NMRNet's exact replay policy unless READ_STATE is selected.
    def __init__(self, api, store, claim, *, refusal_policy=TerminalRefusalPolicy.RETAIN_PENDING, terminal_recovery=None):
        if api.origin != store.scope.api_origin:
            raise ValueError("Cannot configure recovery: signing origin differs from retained storage scope")
        self.api, self.store, self.claim = api, store, claim
        require_refusal_policy(refusal_policy)
        if terminal_recovery is not None and type(terminal_recovery) is not TerminalRecovery:
            raise ValueError("Attempt recovery requires an admitted terminal retry policy.")
        self._refusal_policy = refusal_policy
        self._terminal_recovery = terminal_recovery
        self._used = False
        self._child = None
        self._active = False
        self._observing = False
        self._failure = None
        self._api_failure = None
        self._unretained = None

    @property
    def last_failure(self):
        return self._child.last_failure if self._child is not None else self._failure

    async def recover(self, record):
        start = pending_start(record)
        if self._used or start.scope != self.store.scope:
            raise RecoveryRefused("Recovery requires an unused owner and a record matching the explicitly opened store scope")
        self.claim.require_start(start)
        if type(record) is StartPending:
            self._child = AttemptStarter(self.api, self.store, self.claim)
            self._used = True
            return await self._child.resume(record)
        acknowledged = acknowledged_attempt(record)
        self.claim.require(start.scope, acknowledged.admission.execution_attempt_ref)
        if type(record) is TerminalPending:
            self._child = TerminalPublisher(self.api, self.store, self.claim, refusal_policy=self._refusal_policy)
            self._used = True
            return await self._child.publish_until_resolved(
                record, recovery=self._terminal_recovery, initial_seconds=1
            )
        self.claim.begin(self)
        self._used = self._active = self._observing = True
        try:
            if record not in await self.store.records(owner=self):
                raise RecoveryRefused("Selected recovery record is no longer current; reread storage before another action")
            if type(record) is AcknowledgedAttempt:
                observed = await self._read_attempt(record)
                return ObservedAttempt(record, observed)
            if type(record) is TerminalReadRequired:
                self._unretained = await self._read_terminal(record)
                result = await self.store.replace(record, self._unretained, owner=self)
                self._unretained = None
                return result
            return record
        except asyncio.CancelledError as cause:
            self._failure = RecoveryFailed(cause, api_failure=self._api_failure, observed_record=self._unretained)
            raise
        except Exception as cause:
            # After a read, even an unexpected storage defect must keep the
            # observed record and original cause owned through settlement.
            self._failure = RecoveryFailed(cause, api_failure=self._api_failure, observed_record=self._unretained)
            raise self._failure from None
        finally:
            self._observing = False
            self._finish_if_settled()

    async def _read_attempt(self, record):
        try:
            return await self.api.read_attempt(
                record.admission.execution_attempt_ref, job_ref=record.start.input.job_ref, owner=self
            )
        except asyncio.CancelledError:
            self._api_failure = self.api.last_failure
            raise
        except ApiFailure as cause:
            self._api_failure = cause
            raise

    async def _read_terminal(self, required):
        """Read once; API's admitted Problem owns not-visible classification."""
        try:
            observed = await self._read_attempt(required.pending.attempt)
        except ApiFailure as failure:
            if failure.status == 404 and failure.problem_code == "resource_not_found":
                return TerminalReadNotVisible(required, failure.request_id)
            raise
        return TerminalStateObserved(required, observed)

    async def settle(self):
        """Observe original delegated or read-only I/O; never redispatch recovery."""
        if self._child is not None:
            return await self._child.settle()
        if not self._active or self._observing:
            raise RecoveryRefused("No unobserved recovery operation is available to settle")
        self._observing = True
        cancellation = None
        failures = []
        result = None
        try:
            for port in (self.api, self.store):
                if port.pending_owner is not self:
                    continue
                try:
                    if port is self.api:
                        await port.retire(owner=self)
                    else:
                        settled = await port.settle(owner=self)
                        if self._unretained is not None:
                            if settled.value != self._unretained:
                                raise StoreFailed(
                                    "Settled storage result differs from the owned terminal readback; preserve storage for reconciliation"
                                )
                            result = self._unretained
                            self._unretained = None
                except asyncio.CancelledError as cause:
                    failures.append(cause)
                    cancellation = cause
                except (StoreFailed, StoreRefused, ExchangeFailed) as cause:
                    failures.append(cause)
            if not failures and self._unretained is not None:
                # No new I/O failed: repeat the existing unresolved disposition,
                # rather than accumulating synthetic cleanup failures on polls.
                raise self._failure from None
            if failures:
                original = self._failure
                self._failure = RecoveryFailed(
                    original.cause,
                    (*original.cleanup_failures, *failures),
                    api_failure=original.api_failure,
                    observed_record=self._unretained,
                )
                if cancellation is not None:
                    raise cancellation from None
                raise self._failure from None
            return result
        finally:
            self._observing = False
            self._finish_if_settled()

    def _finish_if_settled(self):
        if self.api.pending_owner is not self and self.store.pending_owner is not self and self._unretained is None:
            self.claim.finish(self)
            self._active = False
