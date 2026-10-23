"""A lost start reply retains the exact restart obligation across storage reopen."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF, start_response
from tests.fakes.publication import api

from provider_core.attempt_record import MAX_RECORD_BYTES, LocalExecutionPhase
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.ownership import AttemptClaims
from provider_core.recovery import AttemptRecovery, RecoveryRefused
from provider_core.starting import AttemptStarter, StartFailed
from provider_core.transport import HttpTransport


class StartingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_start_reply_reopens_and_replays_original_bytes(self):
        async with api_tls_peer("success", response_bodies=[None, start_response(replayed=True)]) as peer:
            recovery_scope = replace(scope(), api_origin=peer.origin)
            pending = replace(record(), scope=recovery_scope)
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            client = api(transport, peer.origin)
            claims = AttemptClaims()
            with tempfile.TemporaryDirectory() as directory:
                store = AttemptStore(
                    Path(directory), scope=recovery_scope, limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=1
                )
                await store.open()
                claim = claims.claim_start(pending)
                try:
                    with self.assertRaises(StartFailed) as lost:
                        await AttemptStarter(client, store, claim).start(pending)
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
                    acknowledged = await recovery.recover(retained)
                    self.assertEqual(await reopened.records(), (acknowledged,))
                    self.assertTrue(acknowledged.admission.replayed)
                    self.assertIs(acknowledged.local_phase, LocalExecutionPhase.NOT_ENTERED)
                    claim.require(recovery_scope, ATTEMPT_REF)
                    self.assertEqual(peer.request_bodies, [pending.body, pending.body])
                    self.assertNotEqual(peer.requests[0], peer.requests[1])
                    with self.assertRaises(RecoveryRefused):
                        await recovery.recover(retained)
                    self.assertEqual(len(peer.requests), 2)
                    claim.release()
                finally:
                    await reopened.close()
            await transport.retire()
