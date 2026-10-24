"""Outcome choice cannot switch, lose its evidence, or accept a foreign receipt."""

import json
import traceback
import unittest
from dataclasses import replace

from provider_core.attempt_record import (
    MAX_EVIDENCE_BYTES,
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
    acknowledge_start,
    decode_record,
    encode_record,
    mark_execution_entered,
    require_transition,
)
from provider_core.attempt_request import complete_body, fail_body
from provider_core.attempt_response import AttemptObservation, read_fail
from tests.fakes.attempt_store import PREPARED_IDENTITY, admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF, fail_response


class TerminalRecordFailureTests(unittest.TestCase):
    def test_read_only_records_cannot_reverse_or_adopt_an_unbound_observation(self):
        pending = self.pending()
        required = TerminalReadRequired(pending, 409, None)
        observed = TerminalStateObserved(required, AttemptObservation(ATTEMPT_REF, "job:example", "succeeded", "closed"))
        invisible = TerminalReadNotVisible(required, "request:read")
        for prior in (required, observed, invisible):
            for target in (pending, pending.attempt, required, observed, invisible):
                if prior is required and target in (observed, invisible):
                    continue
                with self.assertRaises(ValueError):
                    require_transition(prior, target)
        for change in (
            {"job_ref": "job:other"},
            {"execution_attempt_ref": "execution_attempt:sha256:" + "b" * 64},
            {"state": "private-canary"},
            {"job_state": "private-canary"},
        ):
            with self.assertRaises(ValueError) as failure:
                TerminalStateObserved(required, replace(observed.observation, **change))
            self.assertNotIn("private-canary", str(failure.exception))
        for status in (True, 200, 403, 500, "409"):
            with self.assertRaises(ValueError):
                TerminalReadRequired(pending, status, None)
        for correlation in (None, "", "r" * 129, "private-canary\n"):
            with self.assertRaises(ValueError):
                TerminalReadNotVisible(required, correlation)

    def test_read_only_restore_has_closed_schemas_and_preserves_scope(self):
        required = TerminalReadRequired(self.pending(), 404, "request:publication")
        observed = TerminalStateObserved(required, AttemptObservation(ATTEMPT_REF, "job:example", "failed", "open"))
        for value in (required, observed, TerminalReadNotVisible(required, "request:read")):
            raw = encode_record(value)
            document = json.loads(raw)
            for change in ({"extra": "private-canary"}, {"schema_id": "private-canary"}):
                with self.assertRaises(ValueError) as failure:
                    decode_record(json.dumps(document | change).encode(), scope=scope())
                self.assertNotIn("private-canary", str(failure.exception))
            with self.assertRaises(ValueError):
                decode_record(raw, scope=replace(scope(), generation_id="other"))
        for change in ({"observation": {}}, {"read_required": {}}, {"observation": {"private-canary": 1}}):
            with self.assertRaises(ValueError):
                decode_record(json.dumps(json.loads(encode_record(observed)) | change).encode(), scope=scope())

    def test_suppression_cannot_change_its_evidence_reason_or_acknowledgement(self):
        pending = self.pending()
        retained = SuppressedOutcome(pending, SuppressionReason.CALLER_CANCELLED)
        for replacement in (
            pending,
            pending.attempt,
            replace(retained, reason=SuppressionReason.SERVER_STOP),
            replace(retained, candidate=replace(pending, evidence=ProtectedEvidence("evidence.v1", b"changed"))),
        ):
            with self.assertRaises(ValueError):
                require_transition(retained, replacement)
        with self.assertRaises(ValueError):
            require_transition(mark_execution_entered(pending.attempt, PREPARED_IDENTITY), retained)
        document = json.loads(encode_record(retained))
        for reason in ("private untrusted reason", None, 1, {}, []):
            document["reason"] = reason
            with self.assertRaises(ValueError) as failure:
                decode_record(json.dumps(document).encode(), scope=scope())
            self.assertNotIn("private untrusted reason", str(failure.exception))

    def pending(self):
        return TerminalPending(
            acknowledge_start(record(), admission()),
            TerminalOperation.FAIL,
            fail_body(ATTEMPT_REF, "input_rejected", " Explain\n雪\x01 "),
            ProtectedEvidence("evidence.v1", b"protected original"),
        )

    def test_mutable_missing_or_oversized_required_evidence_is_rejected(self):
        for value in (b"", bytearray(b"data"), b"x" * (MAX_EVIDENCE_BYTES + 1)):
            with self.assertRaises(ValueError):
                ProtectedEvidence("evidence.v1", value)
        with self.assertRaises(ValueError):
            replace(self.pending(), evidence=None)

    def test_terminal_choice_or_evidence_cannot_be_replaced(self):
        pending = self.pending()
        for changed in (
            replace(pending, body=fail_body(ATTEMPT_REF, "other_failure", "Different outcome")),
            replace(pending, evidence=ProtectedEvidence("evidence.v1", b"different evidence")),
            replace(pending, attempt=mark_execution_entered(pending.attempt, PREPARED_IDENTITY)),
        ):
            with self.assertRaises(ValueError):
                require_transition(pending, changed)
        with self.assertRaises(ValueError):
            require_transition(pending, pending.attempt)

    def test_foreign_body_receipt_and_unreviewed_hold_are_rejected(self):
        pending = self.pending()
        with self.assertRaises(ValueError):
            replace(pending, body=complete_body(ATTEMPT_REF, "result.v1", b"result"))
        receipt = read_fail(
            fail_response(), execution_attempt_ref=ATTEMPT_REF, failure_code="input_rejected", failure_message=" Explain\n雪\x01 "
        )
        with self.assertRaises(ValueError):
            TerminalConfirmed(pending, replace(receipt, failure_message="different"))
        with self.assertRaises(ValueError):
            TerminalHeld(pending, "private-unreviewed-code", None)
        with self.assertRaises(ValueError):
            replace(pending, attempt=acknowledge_start(record(), admission(state="expired")))

    def test_corrupt_or_foreign_terminal_records_fail_without_exposing_evidence(self):
        document = json.loads(encode_record(self.pending()))
        for changes in (
            {"operation": "private-invalid-operation"},
            {"attempt": {}},
            {"evidence": {}},
            {"evidence": {"schema_id": "evidence.v1", "content": "eB=="}},
            {"body": "eB=="},
            {"extra": "private-canary"},
        ):
            with self.assertRaises(ValueError) as caught:
                decode_record(json.dumps(document | changes).encode(), scope=scope())
            self.assertNotIn("private-canary", str(caught.exception))
        with self.assertRaises(ValueError):
            decode_record(encode_record(self.pending()), scope=replace(scope(), generation_id="foreign"))

    def test_corrupt_receipt_and_hold_decode_preserve_safe_diagnostics(self):
        pending = self.pending()
        receipt = read_fail(
            fail_response(), execution_attempt_ref=ATTEMPT_REF, failure_code="input_rejected", failure_message=" Explain\n雪\x01 "
        )
        confirmed = json.loads(encode_record(TerminalConfirmed(pending, receipt)))
        held = json.loads(encode_record(TerminalHeld(pending, "execution_attempt_outcome_expired", None)))
        cases = (
            confirmed | {"pending": {}},
            confirmed | {"receipt": confirmed["receipt"] | {"failure_message": "private-canary"}},
            held | {"pending": held["pending"] | {"schema_id": "foreign"}},
            held | {"problem_code": "private-canary"},
            held | {"request_id": "private-canary\n"},
        )
        for document in cases:
            raw = json.dumps(document).encode()
            try:
                decode_record(raw, scope=scope())
            except ValueError as error:
                self.assertNotIn("private-canary", "".join(traceback.format_exception(error)))
            else:
                self.fail("Corrupt terminal record was accepted")
