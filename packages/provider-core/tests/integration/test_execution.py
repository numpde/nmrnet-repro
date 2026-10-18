"""Real signed TLS/store lifecycle with a recording scientific-session port."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from provider_core.attempt_record import MAX_RECORD_BYTES, TerminalConfirmed
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.execution import AttemptExecutor
from provider_core.ownership import AttemptClaims
from provider_core.publication import TerminalPublisher
from provider_core.starting import AttemptStarter
from provider_core.transport import HttpTransport
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import complete_response, observation, progress_response, start_response
from tests.fakes.execution import Session
from tests.fakes.job_input import response
from tests.fakes.publication import api
from tests.fakes.tls_peer import TlsPeer


class ExecutionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_prepare_execute_retain_and_publish_under_one_claim(self):
        replies = [
            start_response(),
            observation(),
            response(),
            progress_response(),
            observation(),
            progress_response(phase="running"),
            observation(),
            observation(),
            complete_response(result_schema_id="result.v1"),
        ]
        async with TlsPeer("success", response_bodies=replies) as peer:
            recovery_scope = replace(scope(), api_origin=peer.origin)
            pending = replace(record(), scope=recovery_scope)
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            client = api(transport, peer.origin)
            claim = AttemptClaims().claim_start(pending)
            with tempfile.TemporaryDirectory() as directory:
                store = AttemptStore(
                    Path(directory), scope=recovery_scope, limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1
                )
                await store.open()
                session = Session(store)
                try:
                    acknowledged = await AttemptStarter(client, store, claim).start(pending)
                    terminal = await AttemptExecutor(client, store, claim, session, poll_seconds=1, task_settle_seconds=0.1).run(
                        acknowledged
                    )
                    confirmed = await TerminalPublisher(client, store, claim).publish(terminal)
                    self.assertIsInstance(confirmed, TerminalConfirmed)
                    self.assertEqual(await store.records(), (confirmed,))
                    self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                    self.assertEqual(peer.request_bodies[0], pending.body)
                    self.assertEqual(peer.request_bodies[-1], terminal.body)
                    self.assertEqual(len(peer.requests), 9)
                    claim.release()
                finally:
                    await store.close()
            await transport.retire()
