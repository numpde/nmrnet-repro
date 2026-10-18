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
from provider_core.attempt_record import AcknowledgedAttempt, StartPending, TerminalPending, acknowledged_attempt, pending_start
from provider_core.attempt_response import AttemptObservation
from provider_core.attempt_store import StoreFailed, StoreRefused
from provider_core.publication import TerminalPublisher
from provider_core.starting import AttemptStarter
from provider_core.transport import ExchangeFailed


class RecoveryRefused(RuntimeError):
    """The requested recovery action was not dispatched."""


class RecoveryFailed(RuntimeError):
    """Safe recovery context retaining protected original and cleanup failures."""

    def __init__(self, cause, cleanup_failures=(), *, api_failure=None):
        super().__init__(
            "Cannot finish retained Attempt observation; preserve its record and settle original I/O before continuing"
        )
        self.cause = cause
        self.cleanup_failures = cleanup_failures
        self.api_failure = api_failure


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

    def __init__(self, api, store, claim):
        if api.origin != store.scope.api_origin:
            raise ValueError("Cannot configure recovery: signing origin differs from retained storage scope")
        self.api, self.store, self.claim = api, store, claim
        self._used = False
        self._child = None
        self._active = False
        self._observing = False
        self._failure = None
        self._api_failure = None

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
            self._child = TerminalPublisher(self.api, self.store, self.claim)
            self._used = True
            return await self._child.publish(record)
        self.claim.begin(self)
        self._used = self._active = self._observing = True
        try:
            if record not in await self.store.records(owner=self):
                raise RecoveryRefused("Selected recovery record is no longer current; reread storage before another action")
            if type(record) is AcknowledgedAttempt:
                observed = await self._read_attempt(record)
                return ObservedAttempt(record, observed)
            return record
        except asyncio.CancelledError as cause:
            self._failure = RecoveryFailed(cause, api_failure=self._api_failure)
            raise
        except (RecoveryRefused, ApiFailure, StoreFailed, StoreRefused) as cause:
            self._failure = RecoveryFailed(cause, api_failure=self._api_failure)
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

    async def settle(self):
        """Observe original delegated or read-only I/O; never redispatch recovery."""
        if self._child is not None:
            return await self._child.settle()
        if not self._active or self._observing:
            raise RecoveryRefused("No unobserved recovery operation is available to settle")
        self._observing = True
        cancellation = None
        failures = []
        try:
            for port in (self.api, self.store):
                if port.pending_owner is not self:
                    continue
                try:
                    if port is self.api:
                        await port.retire(owner=self)
                    else:
                        await port.settle(owner=self)
                except asyncio.CancelledError as cause:
                    failures.append(cause)
                    cancellation = cause
                except (StoreFailed, StoreRefused, ExchangeFailed) as cause:
                    failures.append(cause)
            if failures:
                original = self._failure
                self._failure = RecoveryFailed(
                    original.cause, (*original.cleanup_failures, *failures), api_failure=original.api_failure
                )
                if cancellation is not None:
                    raise cancellation from None
                raise self._failure from None
        finally:
            self._observing = False
            self._finish_if_settled()

    def _finish_if_settled(self):
        if self.api.pending_owner is not self and self.store.pending_owner is not self:
            self.claim.finish(self)
            self._active = False
