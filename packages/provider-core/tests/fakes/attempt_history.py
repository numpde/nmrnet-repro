"""Confirmed synthetic failures for archival tests; no API effect is simulated."""

from hashlib import sha256

from provider_core.attempt_record import (
    ProtectedEvidence,
    TerminalConfirmed,
    TerminalOperation,
    TerminalPending,
    acknowledge_start,
)
from provider_core.attempt_request import fail_body
from provider_core.attempt_response import read_fail
from tests.fakes.attempt_store import admission
from tests.fakes.attempts import fail_response


async def retain_confirmed(store, start, *, evidence):
    reference = "execution_attempt:sha256:" + sha256(start.body).hexdigest()
    acknowledged = acknowledge_start(start, admission(execution_attempt_ref=reference))
    pending = TerminalPending(
        acknowledged,
        TerminalOperation.FAIL,
        fail_body(reference, "input_rejected", "Correct the input"),
        ProtectedEvidence("fixture.evidence.v1", evidence),
    )
    receipt = read_fail(
        fail_response(execution_attempt_ref=reference, failure_message="Correct the input"),
        execution_attempt_ref=reference,
        failure_code="input_rejected",
        failure_message="Correct the input",
    )
    confirmed = TerminalConfirmed(pending, receipt)
    await store.admit(start)
    await store.replace(start, acknowledged)
    await store.replace(acknowledged, pending)
    await store.replace(pending, confirmed)
    return confirmed
