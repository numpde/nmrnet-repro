"""Durable terminal publication across lost replies and a reopened store."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempt_store import scope
from tests.fakes.attempts import ATTEMPT_REF, fail_response
from tests.fakes.publication import api, retained_failure

from provider_core.attempt_record import MAX_RECORD_BYTES, TerminalConfirmed
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.ownership import AttemptClaims
from provider_core.publication import PublicationFailed, TerminalPublisher
from provider_core.recovery import AttemptRecovery, RecoveryRefused
from provider_core.transport import HttpTransport


class PublicationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_reply_reopens_and_explicitly_resends_exact_retained_request(self):
        response = fail_response(replayed=True, failure_message=" Explain\n雪\x01 ")
        async with api_tls_peer("success", response_bodies=[None, response]) as peer:
            recovery_scope = replace(scope(), api_origin=peer.origin)
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            client = api(transport, peer.origin)
            claims = AttemptClaims()
            with tempfile.TemporaryDirectory() as directory:
                store, pending = await retained_failure(directory, recovery_scope)
                claim = claims.claim(recovery_scope, ATTEMPT_REF)
                try:
                    with self.assertRaises(PublicationFailed) as lost:
                        await TerminalPublisher(client, store, claim).publish(pending)
                    self.assertTrue(lost.exception.api_failure.attempt_mutation_may_have_committed)
                    self.assertEqual(await store.records(), (pending,))
                    self.assertEqual(peer.request_bodies, [pending.body])
                    claim.release()
                finally:
                    await store.close()
                reopened = AttemptStore(
                    Path(directory), scope=recovery_scope, limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=1
                )
                await reopened.open()
                try:
                    (retained,) = await reopened.records()
                    (claim,) = claims.claim_records((retained,))
                    recovery = AttemptRecovery(client, reopened, claim)
                    confirmed = await recovery.recover(retained)
                    self.assertIsInstance(confirmed, TerminalConfirmed)
                    self.assertTrue(confirmed.receipt.replayed)
                    self.assertEqual(await reopened.records(), (confirmed,))
                    self.assertEqual(peer.request_bodies, [pending.body, pending.body])
                    self.assertEqual(confirmed.pending.evidence, pending.evidence)
                    with self.assertRaises(RecoveryRefused):
                        await recovery.recover(retained)
                    self.assertEqual(len(peer.requests), 2)
                    claim.release()
                finally:
                    await reopened.close()
            await transport.retire()
