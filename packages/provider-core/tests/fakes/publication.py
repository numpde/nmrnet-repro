"""Retained synthetic failure outcomes, independent of publication orchestration."""

import json
from dataclasses import replace
from pathlib import Path

from provider_core.attempt_record import (
    MAX_RECORD_BYTES,
    ProtectedEvidence,
    TerminalOperation,
    TerminalPending,
    TerminalReadRequired,
    acknowledge_start,
)
from provider_core.attempt_request import fail_body
from provider_core.attempt_store import AttemptStore, StoreLimits
from tests.fakes.api import reply
from tests.fakes.attempt_store import admission, record
from tests.fakes.attempts import ATTEMPT_REF


def terminal_refusal(status, *, code=None):
    """Authored refusal; only known Problem triples acquire domain meaning."""
    name = "not-found" if status == 404 else "operation-conflict"
    problem = {
        "type": f"urn:nmr-api:problem:{name}",
        "title": "PRIVATE_TITLE",
        "detail": "PRIVATE_DETAIL",
        "status": status,
        "code": code if code is not None else "unreviewed_conflict",
        "instance": "/provider/v1/execution-attempts/fail",
        "request_id": "request:test",
    }
    return reply(json.dumps(problem).encode(), status=status)


async def retained_read_required(directory, scope):
    store, pending = await retained_failure(directory, scope)
    required = TerminalReadRequired(pending, 409, "request:publication")
    await store.replace(pending, required)
    return store, required


async def retained_failure(directory, scope):
    start = replace(record(), scope=scope)
    active = acknowledge_start(start, admission())
    terminal = TerminalPending(
        active,
        TerminalOperation.FAIL,
        fail_body(ATTEMPT_REF, "input_rejected", " Explain\n雪\x01 "),
        ProtectedEvidence("evidence.v1", b"protected original cause"),
    )
    store = AttemptStore(Path(directory), scope=scope, limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=1)
    await store.open()
    await store.admit(start)
    await store.replace(start, active)
    await store.replace(active, terminal)
    return store, terminal
