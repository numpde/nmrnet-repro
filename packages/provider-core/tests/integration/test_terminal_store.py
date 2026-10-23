"""Maximum terminal obligations and receipts survive installed-store reopening."""

import tempfile
import unittest
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from provider_core.attempt_record import (
    MAX_EVIDENCE_BYTES,
    MAX_PENDING_RECORD_BYTES,
    MAX_RECORD_BYTES,
    ProtectedEvidence,
    TerminalConfirmed,
    TerminalOperation,
    TerminalPending,
    acknowledge_start,
    encode_record,
    mark_execution_entered,
)
from provider_core.attempt_request import complete_body, fail_body
from provider_core.attempt_response import read_complete, read_fail
from provider_core.attempt_store import AttemptStore, StoreLimits
from tests.fakes.attempt_store import PREPARED_IDENTITY, admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF, complete_response, fail_response


class TerminalStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_maximum_completion_and_failure_with_evidence_and_receipt_survive_reopen(self):
        start = record()
        growth = MAX_PENDING_RECORD_BYTES - len(encode_record(start))
        owner = replace(scope(), api_origin=scope().api_origin + "a" * growth)
        start = replace(start, scope=owner)
        evidence = ProtectedEvidence("\U0001f600" * 1024, b"e" * MAX_EVIDENCE_BYTES)
        text = "\U0001f600" * 1024
        result = b"x" * 786432
        for operation in (TerminalOperation.COMPLETE, TerminalOperation.FAIL):
            acknowledged = acknowledge_start(start, admission())
            if operation is TerminalOperation.COMPLETE:
                active = mark_execution_entered(acknowledged, PREPARED_IDENTITY)
                body = complete_body(ATTEMPT_REF, text, result)
                body += b" " * (2097152 - len(body))
                receipt = read_complete(
                    complete_response(
                        result_schema_id=text,
                        result_fingerprint="sha256:" + sha256(result).hexdigest(),
                        result_byte_length=len(result),
                    ),
                    execution_attempt_ref=ATTEMPT_REF,
                    result_schema_id=text,
                    result=result,
                )
            else:
                active = acknowledged
                body = fail_body(ATTEMPT_REF, "input_rejected", text)
                body += b" " * (16384 - len(body))
                receipt = read_fail(
                    fail_response(failure_message=text),
                    execution_attempt_ref=ATTEMPT_REF,
                    failure_code="input_rejected",
                    failure_message=text,
                )
            pending = TerminalPending(active, operation, body, evidence)
            confirmed = TerminalConfirmed(pending, receipt)
            self.assertLessEqual(len(encode_record(confirmed)), MAX_RECORD_BYTES)
            with tempfile.TemporaryDirectory() as directory:
                store = AttemptStore(
                    Path(directory), scope=owner, limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=2
                )
                await store.open()
                try:
                    await store.admit(start)
                    await store.replace(start, acknowledged)
                    if active != acknowledged:
                        await store.replace(acknowledged, active)
                    await store.replace(active, pending)
                    await store.replace(pending, confirmed)
                finally:
                    await store.close()
                reopened = AttemptStore(
                    Path(directory), scope=owner, limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=2
                )
                await reopened.open()
                try:
                    self.assertEqual(await reopened.records(), (confirmed,))
                finally:
                    await reopened.close()
