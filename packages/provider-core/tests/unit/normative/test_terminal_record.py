"""Terminal records preserve one chosen outcome, evidence and matching receipt."""

import unittest

from provider_core.attempt_record import (
    MAX_EVIDENCE_BYTES,
    LocalExecutionPhase,
    ProtectedEvidence,
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
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF, complete_response, fail_response


class TerminalRecordTests(unittest.TestCase):
    def test_exact_terminal_request_evidence_and_receipt_round_trip(self):
        acknowledged = acknowledge_start(record(), admission())
        entered = mark_execution_entered(acknowledged)
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
        entered = mark_execution_entered(acknowledge_start(record(), admission()))
        body = complete_body(ATTEMPT_REF, "result.v1", b"x" * 786432)
        body += b" " * (2097152 - len(body))
        pending = TerminalPending(
            entered, TerminalOperation.COMPLETE, body, ProtectedEvidence("evidence.v1", b"e" * MAX_EVIDENCE_BYTES)
        )
        self.assertEqual(decode_record(encode_record(pending), scope=scope()), pending)
        self.assertEqual(len(pending.body), 2097152)
