"""Terminal records preserve one chosen outcome, evidence and matching receipt."""

import unittest

from provider_core.attempt_record import (
    MAX_EVIDENCE_BYTES,
    MAX_RECORD_BYTES,
    LocalExecutionPhase,
    ProtectedEvidence,
    SuppressedOutcome,
    SuppressionReason,
    TerminalConfirmed,
    TerminalHeld,
    TerminalOperation,
    TerminalPending,
    acknowledge_start,
    decode_record,
    encode_record,
    mark_execution_entered,
    require_transition,
)
from provider_core.attempt_request import complete_body, fail_body
from provider_core.attempt_response import read_complete, read_fail
from tests.fakes.attempt_store import PREPARED_IDENTITY, admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF, complete_response, fail_response


class TerminalRecordTests(unittest.TestCase):
    def test_suppressed_outcome_keeps_exact_bytes_without_a_publishable_state(self):
        for entered in (False, True):
            acknowledged = acknowledge_start(record(), admission())
            if entered:
                acknowledged = mark_execution_entered(acknowledged, PREPARED_IDENTITY)
            candidate = TerminalPending(
                acknowledged,
                TerminalOperation.FAIL,
                fail_body(ATTEMPT_REF, "input_rejected", "Correct the input") + b" \n",
                ProtectedEvidence("evidence.v1", b"private original evidence"),
            )
            for reason in SuppressionReason:
                suppressed = SuppressedOutcome(candidate, reason)
                require_transition(acknowledged, suppressed)
                restored = decode_record(encode_record(suppressed), scope=scope())
                self.assertEqual(restored, suppressed)
                self.assertNotIsInstance(restored, TerminalPending)
                self.assertEqual(restored.candidate.body, candidate.body)
                self.assertNotIn("private original evidence", repr(restored))
                with self.assertRaises(ValueError):
                    require_transition(suppressed, candidate)

    def test_suppression_wrapper_fits_reserved_capacity_with_maximum_request_and_evidence(self):
        identity = ProtectedEvidence("\U0001f600" * 1024, b"i" * MAX_EVIDENCE_BYTES)
        entered = mark_execution_entered(acknowledge_start(record(), admission()), identity)
        body = complete_body(ATTEMPT_REF, "result.v1", b"x" * 786432)
        body += b" " * (2097152 - len(body))
        candidate = TerminalPending(
            entered, TerminalOperation.COMPLETE, body, ProtectedEvidence("\U0001f600" * 1024, b"e" * MAX_EVIDENCE_BYTES)
        )
        suppressed = SuppressedOutcome(candidate, SuppressionReason.OBSERVATION_FAILED)
        encoded = encode_record(suppressed)
        self.assertLessEqual(len(encoded), MAX_RECORD_BYTES)
        self.assertEqual(decode_record(encoded, scope=scope()), suppressed)

    def test_exact_terminal_request_evidence_and_receipt_round_trip(self):
        acknowledged = acknowledge_start(record(), admission())
        entered = mark_execution_entered(acknowledged, PREPARED_IDENTITY)
        evidence = ProtectedEvidence("consumer.evidence.v1", b"protected cause and worker retirement observation")
        for operation, body, receipt in (
            (
                TerminalOperation.COMPLETE,
                complete_body(ATTEMPT_REF, " result\n雪 ", b"result bytes"),
                read_complete(
                    complete_response(),
                    execution_attempt_ref=ATTEMPT_REF,
                    result_schema_id=" result\n雪 ",
                    result=b"result bytes",
                ),
            ),
            (
                TerminalOperation.FAIL,
                fail_body(ATTEMPT_REF, "input_rejected", " Explain\n雪\x01 "),
                read_fail(
                    fail_response(),
                    execution_attempt_ref=ATTEMPT_REF,
                    failure_code="input_rejected",
                    failure_message=" Explain\n雪\x01 ",
                ),
            ),
        ):
            pending = TerminalPending(entered, operation, body + b" \n", evidence)
            confirmed = TerminalConfirmed(pending, receipt)
            require_transition(entered, pending)
            require_transition(pending, confirmed)
            for value in (pending, confirmed):
                self.assertEqual(decode_record(encode_record(value), scope=scope()), value)
                self.assertNotIn("protected cause", repr(value))
            self.assertEqual(confirmed.pending.body, body + b" \n")
            self.assertEqual(confirmed.pending.attempt.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)

    def test_pre_execution_failure_and_durable_conflict_hold_preserve_evidence(self):
        acknowledged = acknowledge_start(record(), admission())
        pending = TerminalPending(
            acknowledged,
            TerminalOperation.FAIL,
            fail_body(ATTEMPT_REF, "input_rejected", "Correct the input"),
            ProtectedEvidence("consumer.evidence.v1", b"original failure; cleanup unconfirmed"),
        )
        held = TerminalHeld(pending, "execution_attempt_outcome_expired", None)
        require_transition(acknowledged, pending)
        require_transition(pending, held)
        self.assertEqual(decode_record(encode_record(held), scope=scope()), held)

    def test_maximum_request_and_evidence_fit_terminal_retention(self):
        identity = ProtectedEvidence("\U0001f600" * 1024, b"i" * MAX_EVIDENCE_BYTES)
        entered = mark_execution_entered(acknowledge_start(record(), admission()), identity)
        body = complete_body(ATTEMPT_REF, "result.v1", b"x" * 786432)
        body += b" " * (2097152 - len(body))
        pending = TerminalPending(
            entered, TerminalOperation.COMPLETE, body, ProtectedEvidence("evidence.v1", b"e" * MAX_EVIDENCE_BYTES)
        )
        self.assertEqual(decode_record(encode_record(pending), scope=scope()), pending)
        self.assertEqual(len(pending.body), 2097152)
