"""Terminal recovery reuses retained publication, never the scientific session."""

import asyncio
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpcore

from provider_core.attempt_record import TerminalConfirmed
from provider_core.controller import AttemptController, TerminalRecovery
from provider_core.ownership import AttemptClaims
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import complete_response
from tests.fakes.controller import SingleRetirementSession, empty_store, lifecycle_replies
from tests.fakes.publication import api


class ControllerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_outage_beyond_thirty_seconds_replays_exact_terminal_bytes_without_new_science(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = SingleRetirementSession(store)
            transport = Transport(
                *lifecycle_replies()[:-1],
                reply(b"maintenance", status=503),
                reply(complete_response(result_schema_id="result.v1", replayed=True)),
            )
            loop = asyncio.get_running_loop()
            real_time = loop.time
            outage_seconds = 0

            def clock():
                return real_time() + outage_seconds

            def observe_retry(*_facts):
                nonlocal outage_seconds
                outage_seconds += 31
            controller = AttemptController(
                api(transport, scope().api_origin),
                store,
                AttemptClaims(),
                session,
                allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                terminal_recovery=TerminalRecovery(0.001, observe_retry, Mock()),
            )
            try:
                with patch.object(loop, "time", side_effect=clock):
                    confirmed = await controller.run(record())
                self.assertIsInstance(confirmed, TerminalConfirmed)
                self.assertEqual(len(transport.calls), 10)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertEqual(outage_seconds, 31)
                self.assertEqual([request.content for request, _ in transport.calls[-2:]], [confirmed.pending.body] * 2)
            finally:
                await store.close()

    async def test_same_controller_recovers_exact_terminal_body_after_unverified_reply_without_repeating_science(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = SingleRetirementSession(store)
            lost = ExchangeFailed("Unverified response", primary=httpcore.RemoteProtocolError("PRIVATE_WIRE"))
            transport = Transport(
                *lifecycle_replies()[:-1],
                lost,
                reply(b"maintenance", status=503),
                reply(complete_response(result_schema_id="result.v1", replayed=True)),
            )
            retry, recovered = Mock(), Mock()
            controller = AttemptController(
                api(transport, scope().api_origin),
                store,
                AttemptClaims(),
                session,
                allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                terminal_recovery=TerminalRecovery(0.002, retry, recovered),
            )
            try:
                confirmed = await controller.run(record())
                self.assertIsInstance(confirmed, TerminalConfirmed)
                self.assertTrue(confirmed.receipt.replayed)
                self.assertEqual(await store.records(), (confirmed,))
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                sent = [request.content for request, _ in transport.calls[-3:]]
                self.assertEqual(sent, [confirmed.pending.body] * 3)
                self.assertEqual(len(transport.calls), 11)
                self.assertEqual(retry.call_count, 2)
                recovered.assert_called_once()
                self.assertIs(recovered.call_args.args[0], confirmed.pending.attempt)
                self.assertIs(recovered.call_args.args[1], retry.call_args_list[0].args[1])
                self.assertIsNone(await controller.settle())
            finally:
                await store.close()
