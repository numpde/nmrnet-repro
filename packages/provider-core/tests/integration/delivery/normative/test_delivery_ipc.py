"""A private producer receives acknowledgement only for its durable exact body."""

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import h11

from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint, DeliveryRejected, DeliveryUnavailable
from provider_core.delivery_ipc import DeliveryInbox, DeliveryRequestRejected
from provider_core.delivery_store import DeliveryFull, DeliveryQueue, QueueLimits
from provider_core.failure_projection import owned_details
from tests.fakes.delivery import outcome


class DeliveryIPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        root = Path(self.scratch.name)
        (root / "ipc").mkdir(mode=0o700)
        (root / "state").mkdir(mode=0o700)
        self.queue = DeliveryQueue(root / "state", limits=QueueLimits(1, 4096, 2, 8192))
        self.addCleanup(self.queue.close)
        self.endpoint = DeliveryEndpoint("https://api.example", "provider:test", root / "ipc/http.sock")
        self.accepted, self.failures = Mock(), Mock()
        self.inbox = DeliveryInbox(
            self.queue, (self.endpoint,), request_seconds=1, on_accepted=self.accepted, on_failure=self.failures
        )
        await self.inbox.start()
        self.addAsyncCleanup(self.inbox.close)
        self.client = DeliveryClient(self.endpoint, exchange_seconds=1, retirement_seconds=0.2)
        self.addAsyncCleanup(self.client.retire)

    async def test_submit_and_inventory_prove_durability_without_exporting_payload(self):
        item = outcome()
        accepted = await self.client.submit(item)
        self.assertEqual(accepted, item.acceptance())
        self.assertEqual(self.queue.next_pending(item.api_origin, item.provider_ref), item)
        pending = await self.client.pending()
        self.assertEqual(pending["items"][0]["body_sha256"], item.body_sha256)
        self.assertNotIn("SCIENTIFIC_PAYLOAD_NEEDLE", json.dumps(pending))
        self.assertEqual(await self.client.status(item.execution_attempt_ref), pending["items"][0])
        self.assertEqual(await self.client.submit(item), accepted)
        self.assertEqual(len(self.queue.inventory(item.api_origin, item.provider_ref)), 1)
        self.assertTrue(self.client.retirement_confirmed)

    async def test_full_queue_refuses_new_outcome_but_allows_identical_retry(self):
        item = outcome()
        await self.client.submit(item)
        with self.assertRaises(DeliveryFull):
            await self.client.submit(outcome(suffix="1"))
        self.assertEqual(await self.client.submit(item), item.acceptance())
        self.assertEqual(self.queue.next_pending(item.api_origin, item.provider_ref), item)

    async def test_lost_acknowledgement_does_not_change_or_duplicate_the_committed_body(self):
        item = outcome()
        respond = self.inbox._respond
        dropped = False

        async def lose_first(connection, writer, status, document):
            nonlocal dropped
            if not dropped and status == 200 and document == item.acceptance():
                dropped = True
                writer.close()
                await writer.wait_closed()
                return
            await respond(connection, writer, status, document)

        self.inbox._respond = lose_first
        with self.assertRaises(DeliveryUnavailable):
            await self.client.submit(item)
        await self.client.retire()
        self.assertEqual(await self.client.submit(item), item.acceptance())
        self.assertEqual(self.queue.next_pending(item.api_origin, item.provider_ref), item)
        self.assertEqual(len(self.queue.inventory(item.api_origin, item.provider_ref)), 1)

    async def test_wrong_binding_explains_refusal_to_operator_without_echoing_request(self):
        endpoint = DeliveryEndpoint("https://private-canary.example", "provider:private-canary", self.endpoint.socket_path)
        client = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2)
        self.addAsyncCleanup(client.retire)
        with self.assertRaises(DeliveryRejected):
            await client.pending()
        cause, facts = self.failures.call_args.args
        self.assertIs(type(cause), DeliveryRequestRejected)
        self.assertIn("another API/provider", str(cause))
        self.assertEqual(owned_details(cause)["message"], str(cause))
        self.assertNotIn("private-canary", str(cause))
        self.assertEqual(facts["provider_ref"], self.endpoint.provider_ref)
        self.assertEqual(facts["event"], "delivery_request_rejected")
        self.assertEqual(self.queue.inventory(self.endpoint.api_origin, self.endpoint.provider_ref), [])

    async def test_lost_status_response_does_not_claim_durable_enqueue(self):
        item = outcome()
        await self.client.submit(item)

        async def lose_status(connection, writer, status, document):
            raise ConnectionResetError()

        self.inbox._respond = lose_status
        with self.assertRaises(DeliveryUnavailable) as lost:
            await self.client.status(item.execution_attempt_ref)
        self.assertIn("read delivery status", str(lost.exception))
        self.assertNotIn("retain", str(lost.exception).lower())
        _, facts = self.failures.call_args.args
        self.assertFalse(facts["durable_enqueue_confirmed"])
        self.assertEqual(self.queue.next_pending(item.api_origin, item.provider_ref), item)

    async def test_shutdown_joins_committing_write_after_waiter_cancellation(self):
        started, release = threading.Event(), threading.Event()
        item = outcome()

        def blocked_enqueue():
            started.set()
            if not release.wait(2):
                raise TimeoutError("Test did not release the retained SQLite operation.")
            return self.queue.enqueue(item)

        waiter = asyncio.create_task(self.inbox.storage(blocked_enqueue))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        closing = asyncio.create_task(self.inbox.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        closing = asyncio.create_task(self.inbox.close())
        await asyncio.sleep(0)
        self.assertFalse(closing.done())
        release.set()
        await closing
        self.assertEqual(self.queue.next_pending(item.api_origin, item.provider_ref), item)

    async def test_stalled_connection_close_is_aborted_at_the_owned_deadline(self):
        self.inbox.request_seconds = 0.01
        request = h11.Request(method="GET", target="/pending", headers=[("Host", "delivery.invalid")])
        self.inbox._read = AsyncMock(return_value=(request, b""))
        self.inbox._dispatch = AsyncMock(return_value={})
        self.inbox._respond = AsyncMock()

        async def stalled_close():
            await asyncio.Event().wait()

        writer = Mock()
        writer.wait_closed = stalled_close
        await asyncio.wait_for(self.inbox._client(self.endpoint, None, writer), 1)
        writer.transport.abort.assert_called_once()
        cause, facts = self.failures.call_args.args
        self.assertIsInstance(cause, TimeoutError)
        self.assertEqual(facts["event"], "delivery_ipc_close_failed")
