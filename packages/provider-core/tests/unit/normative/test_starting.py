"""A returned start acknowledgement is durable and owned, never execution permission."""

import tempfile
import unittest
from pathlib import Path

from provider_core.attempt_record import MAX_RECORD_BYTES, LocalExecutionPhase
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.starting import AttemptStarter
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF, start_response
from tests.fakes.publication import api


class StartingTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_and_terminal_replay_admissions_are_durable_and_keep_claims(self):
        for state in ("in_progress", "succeeded", "failed", "expired"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                pending = record()
                transport = Transport(reply(start_response(state=state, replayed=state != "in_progress")))
                claims = AttemptClaims()
                claim = claims.claim_start(pending)
                store = AttemptStore(
                    Path(directory), scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=1
                )
                await store.open()
                try:
                    acknowledged = await AttemptStarter(api(transport, scope().api_origin), store, claim).start(pending)
                    self.assertEqual(await store.records(), (acknowledged,))
                    self.assertEqual(acknowledged.admission.state, state)
                    self.assertIs(acknowledged.local_phase, LocalExecutionPhase.NOT_ENTERED)
                    self.assertEqual(transport.calls[0][0].content, pending.body)
                    claim.require(scope(), ATTEMPT_REF)
                    with self.assertRaises(OwnershipRefused):
                        claims.claim_start(pending)
                    claim.release()
                finally:
                    await store.close()
