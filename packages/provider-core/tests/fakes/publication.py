"""Retained synthetic failure outcomes, independent of publication orchestration."""

from dataclasses import replace
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.attempt_record import (
    MAX_RECORD_BYTES,
    ProtectedEvidence,
    TerminalOperation,
    TerminalPending,
    acknowledge_start,
)
from provider_core.attempt_request import fail_body
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.signing import SigningIdentity
from tests.fakes.attempt_store import admission, record
from tests.fakes.attempts import ATTEMPT_REF


def api(transport, origin):
    return ProviderAPI(
        SigningIdentity(origin, "credential:test", Ed25519PrivateKey.generate()), transport, expected_topology="dev-local"
    )


async def retained_failure(directory, scope):
    start = replace(record(), scope=scope)
    active = acknowledge_start(start, admission())
    terminal = TerminalPending(
        active,
        TerminalOperation.FAIL,
        fail_body(ATTEMPT_REF, "input_rejected", " Explain\n雪\x01 "),
        ProtectedEvidence("evidence.v1", b"protected original cause"),
    )
    store = AttemptStore(Path(directory), scope=scope, limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1)
    await store.open()
    await store.admit(start)
    await store.replace(start, active)
    await store.replace(active, terminal)
    return store, terminal
