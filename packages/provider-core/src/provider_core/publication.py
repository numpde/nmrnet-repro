"""Publish one durable terminal obligation, preserving unresolved effect ownership.

Pinned precedents: NMRPeak 1697dc84dd1d76ae25b04efc8183fd80614000e5,
nmrpeak_provider/attempt_journal.py::prepared_terminal_replay; SECS
34f5915c1534278bbf61b4c6592afa44cf94ab08, src/secs_inference/provider/execution.py::_publish.
No retry loop, science callback, record deletion or worker release belongs here.
"""

from provider_core.container_guard import require_container

require_container()

import asyncio

from provider_core.api import ApiFailure
from provider_core.attempt_record import TerminalConfirmed, TerminalHeld, TerminalOperation, TerminalPending
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


class TerminalPublisher:
    """Borrow a lifecycle claim without releasing it or authorizing resource reuse.

    Composition shares the claim registry across all lanes and recovery and routes
    terminal mutations through the owner holding that claim. Low-level store/API
    methods are effect primitives, not alternate orchestration entrypoints.
    """

    def __init__(self, api, store, claim):
        if api.origin != store.scope.api_origin:
            raise ValueError("Cannot configure terminal publication: signing origin differs from retained storage scope")
        self.api = api
        self.store = store
        self.claim = claim
        self._pending = None
        self._unretained = None
        self._api_failure = None
        self._observing = False
        self.last_failure = None

    async def publish(self, pending):
        """Send the retained outcome once and durably record the API response.

        Return TerminalConfirmed for a matching receipt, or TerminalHeld for a
        recognized conflict that blocks replay. A held return does not confirm
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
            result = await self.store.replace(pending, self._unretained, owner=self)
            self._unretained = None
            return result
        except asyncio.CancelledError as cause:
            self._remember(
                "Terminal publication was cancelled; retain the original request and settle owned I/O before reuse", cause
            )
            raise
        except (ApiFailure, StoreFailed, StoreRefused) as cause:
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
                except ValueError:
                    # A conflict for another operation is not permission to hold
                    # this one; preserve the actual API failure unchanged.
                    pass
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
                raise StoreFailed(
                    "Terminal receipt or hold was not durably retained; storage reconciliation is required and the Attempt claim remains held"
                )
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
        failure = PublicationFailed(
            message,
            cause=original.cause if original is not None else cause,
            outcome=self._unretained,
            api_failure=self._api_failure,
            settlement_failures=(*original.settlement_failures, cause) if original is not None else (cause,),
        )
        self.last_failure = failure
        return failure
