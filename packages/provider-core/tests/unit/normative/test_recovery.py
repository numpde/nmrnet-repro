"""Recovery reads acknowledged records without creating a scientific session."""

import tempfile
import unittest
from pathlib import Path

from provider_core.attempt_record import (
    MAX_RECORD_BYTES,
    ProtectedEvidence,
    SuppressedOutcome,
    SuppressionReason,
    TerminalConfirmed,
    TerminalHeld,
    TerminalOperation,
    TerminalPending,
    TerminalReadNotVisible,
    TerminalReadRequired,
    TerminalStateObserved,
    mark_execution_entered,
)
from provider_core.attempt_request import fail_body
from provider_core.attempt_response import read_fail
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.publication import PublicationFailed, PublicationRefused, TerminalPublisher, TerminalRefusalPolicy
from provider_core.recovery import AttemptRecovery, ObservedAttempt
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import PREPARED_IDENTITY, scope
from tests.fakes.attempts import ATTEMPT_REF, fail_response, observation
from tests.fakes.execution import acknowledged_store
from tests.fakes.publication import api, retained_failure, terminal_refusal


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_only_policy_survives_reopen_and_observed_success_is_not_a_receipt(self):
        for status in (404, 409):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                store, pending = await retained_failure(directory, scope())
                claim = AttemptClaims().claim_record(pending)
                code = "resource_not_found" if status == 404 else None
                transport = Transport(terminal_refusal(status, code=code))
                required = await TerminalPublisher(
                    api(transport, scope().api_origin), store, claim, refusal_policy=TerminalRefusalPolicy.READ_STATE
                ).publish(pending)
                self.assertIsInstance(required, TerminalReadRequired)
                self.assertEqual(required.pending, pending)
                self.assertEqual(await store.records(), (required,))
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(transport.calls[0][0].content, pending.body)
                claim.release()
                await store.close()
                reopened = AttemptStore(
                    Path(directory), scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1
                )
                await reopened.open()
                try:
                    (restored,) = await reopened.records()
                    claim = AttemptClaims().claim_record(restored)
                    reads = Transport(reply(observation(state="succeeded", job_state="closed")))
                    client = api(reads, scope().api_origin)
                    observed = await AttemptRecovery(client, reopened, claim).recover(restored)
                    self.assertIsInstance(observed, TerminalStateObserved)
                    self.assertEqual(observed.observation.state, "succeeded")
                    self.assertEqual(observed.pending, pending)
                    self.assertEqual(await reopened.records(), (observed,))
                    self.assertEqual(len(reads.calls), 1)
                    self.assertEqual(reads.calls[0][0].method, "GET")
                    claim.release()
                finally:
                    await reopened.close()
                reopened = AttemptStore(
                    Path(directory), scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1
                )
                await reopened.open()
                try:
                    (restored,) = await reopened.records()
                    claim = AttemptClaims().claim_record(restored)
                    empty = Transport()
                    client = api(empty, scope().api_origin)
                    self.assertEqual(await AttemptRecovery(client, reopened, claim).recover(restored), observed)
                    for candidate in (restored, restored.pending):
                        with self.assertRaises(PublicationRefused):
                            await TerminalPublisher(client, reopened, claim).publish(candidate)
                    self.assertEqual(empty.calls, [])
                    claim.release()
                finally:
                    await reopened.close()

    async def test_reviewed_not_found_read_is_retained_without_inferring_nonexistence(self):
        with tempfile.TemporaryDirectory() as directory:
            store, pending = await retained_failure(directory, scope())
            required = TerminalReadRequired(pending, 409, None)
            await store.replace(pending, required)
            claim = AttemptClaims().claim_record(required)
            transport = Transport(terminal_refusal(404, code="resource_not_found"))
            try:
                result = await AttemptRecovery(api(transport, scope().api_origin), store, claim).recover(required)
                self.assertEqual(result, TerminalReadNotVisible(required, "request:test"))
                self.assertEqual(await store.records(), (result,))
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(transport.calls[0][0].method, "GET")
                claim.release()
            finally:
                await store.close()

    async def test_default_publication_policy_preserves_existing_explicit_replay_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            store, pending = await retained_failure(directory, scope())
            claim = AttemptClaims().claim_record(pending)
            transport = Transport(terminal_refusal(404, code="resource_not_found"))
            try:
                with self.assertRaises(PublicationFailed):
                    await TerminalPublisher(api(transport, scope().api_origin), store, claim).publish(pending)
                self.assertEqual(await store.records(), (pending,))
                self.assertEqual(len(transport.calls), 1)
                claim.release()
            finally:
                await store.close()

    async def test_reopened_suppressed_outcome_and_nested_candidate_cannot_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, original_claim = await acknowledged_store(directory)
            candidate = TerminalPending(
                acknowledged,
                TerminalOperation.FAIL,
                fail_body(ATTEMPT_REF, "input_rejected", "Correct the input"),
                ProtectedEvidence("evidence.v1", b"private evidence"),
            )
            retained = SuppressedOutcome(candidate, SuppressionReason.SERVER_STOP)
            await store.replace(acknowledged, retained)
            original_claim.release()
            await store.close()
            reopened = AttemptStore(
                Path(directory), scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1
            )
            await reopened.open()
            try:
                (restored,) = await reopened.records()
                claim = AttemptClaims().claim_record(restored)
                transport = Transport()
                client = api(transport, scope().api_origin)
                self.assertEqual(await AttemptRecovery(client, reopened, claim).recover(restored), retained)
                for forbidden in (restored, restored.candidate):
                    with self.assertRaises(PublicationRefused):
                        await TerminalPublisher(client, reopened, claim).publish(forbidden)
                self.assertEqual(await reopened.records(), (retained,))
                self.assertEqual(transport.calls, [])
                claim.release()
            finally:
                await reopened.close()

    async def test_confirmed_and_held_records_return_unchanged_without_requests(self):
        for state in ("confirmed", "held"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                store, pending = await retained_failure(directory, scope())
                if state == "confirmed":
                    receipt = read_fail(
                        fail_response(),
                        execution_attempt_ref=ATTEMPT_REF,
                        failure_code="input_rejected",
                        failure_message=" Explain\n雪\x01 ",
                    )
                    retained = TerminalConfirmed(pending, receipt)
                else:
                    retained = TerminalHeld(pending, "execution_attempt_outcome_expired", None)
                await store.replace(pending, retained)
                claim = AttemptClaims().claim_record(retained)
                transport = Transport()
                recovery = AttemptRecovery(api(transport, store.scope.api_origin), store, claim)
                try:
                    self.assertEqual(await recovery.recover(retained), retained)
                    self.assertEqual(await store.records(), (retained,))
                    self.assertEqual(transport.calls, [])
                    claim.require_start(pending.attempt.start)
                    claim.require(store.scope, ATTEMPT_REF)
                    claim.release()
                finally:
                    await store.close()

    async def test_entered_record_readback_preserves_phase_and_both_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, original_claim = await acknowledged_store(directory)
            entered = mark_execution_entered(acknowledged, PREPARED_IDENTITY)
            await store.replace(acknowledged, entered)
            original_claim.release()
            claims = AttemptClaims()
            (claim,) = claims.claim_records(await store.records())
            transport = Transport(reply(observation()))
            recovery = AttemptRecovery(api(transport, store.scope.api_origin), store, claim)
            try:
                observed = await recovery.recover(entered)
                self.assertIsInstance(observed, ObservedAttempt)
                self.assertEqual(observed.record, entered)
                self.assertEqual(observed.observation.state, "in_progress")
                self.assertEqual(await store.records(), (entered,))
                self.assertEqual(len(transport.calls), 1)
                with self.assertRaises(OwnershipRefused):
                    claims.claim_start(entered.start)
                with self.assertRaises(OwnershipRefused):
                    claims.claim(store.scope, ATTEMPT_REF)
                claim.release()
            finally:
                await store.close()
