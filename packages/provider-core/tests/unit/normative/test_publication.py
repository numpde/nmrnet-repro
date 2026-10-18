"""Publication sends only retained bytes and returns only durable receipt/hold state."""

import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.attempt_record import (
    MAX_RECORD_BYTES,
    ProtectedEvidence,
    TerminalConfirmed,
    TerminalOperation,
    TerminalPending,
    acknowledge_start,
)
from provider_core.attempt_request import fail_body
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.ownership import AttemptClaims
from provider_core.publication import TerminalPublisher
from provider_core.signing import SigningIdentity
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF, fail_response


class PublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_retained_failure_gets_durable_receipt_without_releasing_claim(self):
        start = record()
        active = acknowledge_start(start, admission())
        terminal = TerminalPending(
            active,
            TerminalOperation.FAIL,
            fail_body(ATTEMPT_REF, "input_rejected", " Explain\n雪\x01 ") + b"\n",
            ProtectedEvidence("evidence.v1", b"protected original"),
        )
        transport = Transport(reply(fail_response()))
        api = ProviderAPI(SigningIdentity("https://api.example", "credential:test", Ed25519PrivateKey.generate()), transport)
        claims = AttemptClaims()
        claim = claims.claim(scope(), ATTEMPT_REF)
        with tempfile.TemporaryDirectory() as directory:
            store = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1)
            await store.open()
            try:
                await store.admit(start)
                await store.replace(start, active)
                await store.replace(active, terminal)
                publisher = TerminalPublisher(api, store, claim)
                result = await publisher.publish(terminal)
                self.assertIsInstance(result, TerminalConfirmed)
                self.assertEqual(await store.records(), (result,))
                self.assertEqual(transport.calls[0][0].content, terminal.body)
                self.assertEqual(len(transport.calls), 1)
                self.assertTrue(claim.active)
                claim.release()
            finally:
                await store.close()
