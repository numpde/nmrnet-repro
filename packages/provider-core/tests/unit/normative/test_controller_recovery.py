"""Terminal recovery reuses retained publication, never the scientific session."""

import asyncio
import tempfile
import unittest
from unittest.mock import Mock

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
    async def test_admitted_retry_may_finish_its_bounded_io_after_retry_window(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = SingleRetirementSession(store)
            transport = Transport(
                *lifecycle_replies()[:-1],
                reply(b"maintenance", status=503),
                reply(complete_response(result_schema_id="result.v1", replayed=True)),
            )
            original_exchange = transport.exchange

            async def delayed_receipt(request, *, maximum_bytes, owner=None):
                response = await original_exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                if len(transport.calls) == 10:
                    await asyncio.sleep(0.15)
                return response

            transport.exchange = delayed_receipt
            controller = AttemptController(
                api(transport, scope().api_origin),
                store,
                AttemptClaims(),
                session,
                allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                terminal_recovery=TerminalRecovery(0.1, 0.001, Mock(), Mock()),
            )
            try:
                confirmed = await controller.run(record())
                self.assertIsInstance(confirmed, TerminalConfirmed)
                self.assertEqual(len(transport.calls), 10)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
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
                terminal_recovery=TerminalRecovery(1, 0.002, retry, recovered),
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
