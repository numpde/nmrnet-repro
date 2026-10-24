"""Publish one durable terminal obligation, preserving unresolved effect ownership.

Pinned precedents: NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_journal.py::prepared_terminal_replay; SECS
34f5915c1534278bbf61b4c6592afa44cf94ab08, src/secs_inference/provider/execution.py::_publish.
Reviewed exact-request pacing belongs to this owner; no science callback,
record deletion or worker release belongs here.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from provider_core.api import ApiFailure
from provider_core.attempt_record import AcknowledgedAttempt, TerminalConfirmed, TerminalHeld, TerminalOperation, TerminalPending, TerminalReadRequired
from provider_core.attempt_store import StoreFailed, StoreRefused
from provider_core.transport import ExchangeFailed


class PublicationRefused(RuntimeError):
    """No publication was dispatched by this call."""


class PublicationFailed(RuntimeError):
    """Safe effect context; original failures and unretained outcomes are protected."""

    def __init__(self, message, *, cause, outcome, api_failure, settlement_failures):
        super().__init__(message)
        self.cause = cause
        self.outcome = outcome
        self.api_failure = api_failure
        self.settlement_failures = settlement_failures


class TerminalRefusalPolicy(Enum):
    """Consumer choice for terminal HTTP 404/409 without an operation-valid hold.

    RETAIN_PENDING preserves NMRNet's explicit exact-request replay contract.
    READ_STATE follows SECS's no-resend reconciliation; it does not claim the
    failed send had no effects. Other statuses and transport failures without
    a received HTTP 404/409 head are unchanged.
    """

    RETAIN_PENDING = "retain_pending"
    READ_STATE = "read_state"


@dataclass(frozen=True, slots=True)
class TerminalRecovery:
    """Pace reviewed exact publication retries until resolution or cancellation.

    Callers establish native retirement before using this policy. Individual
    exchanges and settlement remain bounded; availability waits do not expire
    the durable command. Observers receive its acknowledgement and failed send.
    """

    backoff_max_seconds: float
    on_retry: Callable[[AcknowledgedAttempt, ApiFailure, float], None] = field(repr=False)
    on_recovered: Callable[[AcknowledgedAttempt, ApiFailure], None] = field(repr=False)

    def __post_init__(self):
        value = self.backoff_max_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError("Terminal recovery backoff maximum must be finite positive seconds")
        if not callable(self.on_retry) or not callable(self.on_recovered):
            raise ValueError("Terminal recovery requires explicit protected retry and recovery observers")


def require_refusal_policy(policy):
    if type(policy) is not TerminalRefusalPolicy:
        raise ValueError("Terminal refusal policy must explicitly retain pending publication or require read-only state recovery")


class TerminalPublisher:
    """Borrow a lifecycle claim without releasing it or authorizing resource reuse.

    Composition shares the claim registry across all lanes and recovery and routes
    terminal mutations through the owner holding that claim. Low-level store/API
    methods are effect primitives, not alternate orchestration entrypoints.
    """

    # silent-defaults: allow-contract -- Existing NMRNet consumers retain pending requests for explicit exact replay; SECS selects read-only reconciliation for unclassified HTTP 404/409.
    def __init__(self, api, store, claim, *, refusal_policy=TerminalRefusalPolicy.RETAIN_PENDING):
        if api.origin != store.scope.api_origin:
            raise ValueError("Cannot configure terminal publication: signing origin differs from retained storage scope")
        self.api = api
        self.store = store
        self.claim = claim
        require_refusal_policy(refusal_policy)
        self._refusal_policy = refusal_policy
        self._pending = None
        self._unretained = None
        self._api_failure = None
        self._observing = False
        self.last_failure = None

    async def publish_until_resolved(self, pending, *, recovery, initial_seconds):
        """Pace only replayable failures after this publisher settles original I/O.

        Cancellation ends availability waiting, preserving the durable request
        and this owner's latest failure. Neither a received-but-unretained outcome
        nor failed cleanup grants another send.
        """
        if recovery is None:
            return await self.publish(pending)
        if type(recovery) is not TerminalRecovery:
            raise ValueError("Terminal publication requires an admitted recovery policy.")
        if type(initial_seconds) not in (int, float) or not math.isfinite(initial_seconds) or initial_seconds <= 0:
            raise ValueError("Terminal retry initial delay must be finite positive seconds.")
        first_failure = None
        delay = min(initial_seconds, recovery.backoff_max_seconds)
        while True:
            try:
                result = await self.publish(pending)
            except PublicationFailed as failure:
                if (
                    type(failure) is not PublicationFailed
                    or failure.cause is not failure.api_failure
                    or failure.api_failure is None
                    or failure.outcome is not None
                    or failure.settlement_failures
                ):
                    raise
                if self.claim.operation_owner is self:
                    await self.settle()
                if not self.api.can_replay_terminal(failure.api_failure):
                    raise
                if first_failure is None:
                    first_failure = failure.api_failure
                seconds = random.uniform(delay / 2, delay)
                recovery.on_retry(pending.attempt, failure.api_failure, seconds)
                await asyncio.sleep(seconds)
                delay = min(recovery.backoff_max_seconds, delay * 2)
            else:
                if first_failure is not None and type(result) is TerminalConfirmed:
                    recovery.on_recovered(pending.attempt, first_failure)
                return result

    async def publish(self, pending):
        """Send the retained outcome once and durably record the API response.

        Return TerminalConfirmed for a matching receipt, or TerminalHeld for a
        recognized conflict that blocks replay. With READ_STATE, other HTTP
        404/409 responses become durable TerminalReadRequired before any GET.
        A held or read-required return does not confirm
        that the requested outcome was accepted. Other failures raise; unresolved
        I/O and outcomes not yet durably recorded remain owned until settlement
        or reconciliation.
        """
        if type(pending) is not TerminalPending:
            raise PublicationRefused(
                "Only a retained pending terminal outcome can be published; confirmed or held records cannot be resent"
            )
        if self._pending is not None:
            raise PublicationRefused(
                "Terminal publisher still owns an unresolved operation; settle it before another publication"
            )
        self.claim.require(self.store.scope, pending.attempt.admission.execution_attempt_ref)
        if pending.attempt.start.scope != self.store.scope:
            raise PublicationRefused("Terminal record belongs to another recovery scope")
        self.claim.begin(self)
        self._pending = pending
        self._observing = True
        self._api_failure = None
        self.last_failure = None
        try:
            if pending not in await self.store.records(owner=self):
                raise PublicationRefused(
                    "Exact terminal request and required evidence are not the current durable record; reread storage before publication"
                )
            self._unretained = await self._send(pending)
            if type(self._unretained) is TerminalReadRequired:
                # The durable disposition is deliberately small. Keep the failed
                # send's protected cause available even after another API call.
                self._remember(
                    "Terminal publication stopped for read-only reconciliation; the original send remains unconfirmed",
                    self._api_failure,
                )
            result = await self.store.replace(pending, self._unretained, owner=self)
            self._unretained = None
            if type(result) is TerminalReadRequired and self.api.pending_owner is self:
                # Retaining a no-resend decision does not settle the failed send.
                # Propagate its original causes while the same owner retires I/O.
                raise self.last_failure
            return result
        except asyncio.CancelledError as cause:
            self._remember(
                "Terminal publication was cancelled; retain the original request and settle owned I/O before reuse", cause
            )
            raise
        except (PublicationRefused, PublicationFailed):
            raise
        except Exception as cause:
            # A storage defect after receiving an outcome still needs the same
            # protected evidence and ownership as an expected I/O failure.
            raise self._remember(
                "Terminal publication lacks durable confirmation; preserve the retained request and protected effect evidence",
                cause,
            ) from None
        finally:
            self._observing = False
            self._finish_if_settled()

    async def _send(self, pending):
        reference = pending.attempt.admission.execution_attempt_ref
        try:
            if pending.operation is TerminalOperation.COMPLETE:
                receipt = await self.api.complete_attempt(pending.body, execution_attempt_ref=reference, owner=self)
            else:
                receipt = await self.api.fail_attempt(pending.body, execution_attempt_ref=reference, owner=self)
        except asyncio.CancelledError:
            self._api_failure = self.api.last_failure
            raise
        except ApiFailure as failure:
            self._api_failure = failure
            if failure.status == 409 and not failure.attempt_mutation_may_have_committed:
                try:
                    return TerminalHeld(pending, failure.problem_code, failure.request_id)
                # silent-defaults: allow-contract -- A conflict for a different operation cannot establish a hold; keep the API failure for the selected refusal policy.
                except ValueError:
                    # A conflict for another operation is not permission to hold
                    # this one; preserve the actual API failure unchanged.
                    pass
            if self._refusal_policy is TerminalRefusalPolicy.READ_STATE and failure.status in (404, 409):
                return TerminalReadRequired(pending, failure.status, failure.request_id)
            raise
        return TerminalConfirmed(pending, receipt)

    async def settle(self):
        """Observe original I/O only; never resend or silently repeat a failed write."""
        if self._pending is None or self._observing:
            raise PublicationRefused("No unobserved publication operation is available to settle")
        self._observing = True
        try:
            if self.api.pending_owner is self:
                await self.api.retire(owner=self)
            result = None
            if self.store.pending_owner is self:
                settled = await self.store.settle(owner=self)
                if self._unretained is not None:
                    if settled.value != self._unretained:
                        raise StoreFailed(
                            "Settled storage result differs from the owned terminal outcome; preserve storage for reconciliation"
                        )
                    result = self._unretained
                    self._unretained = None
            if self._unretained is not None:
                # No original write remains to observe. Keep the same storage
                # failure and claim rather than inventing failures on each poll.
                raise self.last_failure from None
            return result
        except asyncio.CancelledError as cause:
            self._remember_settlement("Terminal settlement was cancelled; original effect ownership remains retained", cause)
            raise
        except (StoreFailed, StoreRefused, ExchangeFailed) as cause:
            failure = self._remember_settlement(
                "Cannot settle terminal publication; preserve primary evidence and the Attempt claim",
                cause,
            )
            raise failure from None
        finally:
            self._observing = False
            self._finish_if_settled()

    def _finish_if_settled(self):
        if self.api.pending_owner is not self and self.store.pending_owner is not self and self._unretained is None:
            self.claim.finish(self)
            self._pending = None

    def _remember(self, message, cause):
        failure = PublicationFailed(
            message, cause=cause, outcome=self._unretained, api_failure=self._api_failure, settlement_failures=()
        )
        self.last_failure = failure
        return failure

    def _remember_settlement(self, message, cause):
        """Keep the publication failure primary through repeated cleanup attempts."""
        original = self.last_failure
        outcome = self._unretained
        if outcome is None and original is not None:
            outcome = original.outcome
        settlement_failures = (cause,)
        if original is not None:
            settlement_failures = (*original.settlement_failures, cause)
        failure = PublicationFailed(
            message,
            cause=original.cause if original is not None else cause,
            outcome=outcome,
            api_failure=self._api_failure,
            settlement_failures=settlement_failures,
        )
        self.last_failure = failure
        return failure
