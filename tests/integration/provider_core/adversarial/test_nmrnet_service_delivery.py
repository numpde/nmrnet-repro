"""Idle relay read recovery re-enters one complete view without admitting work."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from nmrnet_provider.service import ProviderService
from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint, DeliveryRejected, DeliveryUnavailable
from provider_core.transport import ExchangeFailed


class IdleDeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.endpoint = DeliveryEndpoint("https://api.example", "provider:example", Path(self.scratch.name) / "relay.sock")
        self.client = DeliveryClient(self.endpoint, exchange_seconds=0.2, retirement_seconds=0.2)
        self.addAsyncCleanup(self.client.retire)
        self.service = ProviderService.__new__(ProviderService)
        self.service.api = SimpleNamespace(last_failure=None, can_retry_read=Mock(return_value=False), retire=AsyncMock())
        self.service.delivery = self.client
        self.service.policy = SimpleNamespace(idle_seconds=0.01, api_backoff_max_seconds=0.02)
        self.service._stop = asyncio.Event()
        self.service._failures = []
        self.service.last_failure = None
        self.service._on_api_retry, self.service._on_api_recovered = Mock(), Mock()
        self.requests = []
        self.server = None
        self.handlers = set()
        self.status = 200
        self.addAsyncCleanup(self.close_peer)

    async def start_peer(self):
        self.server = await asyncio.start_unix_server(self.handle, str(self.endpoint.socket_path))

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            self.requests.append(await reader.readuntil(b"\r\n\r\n"))
            body = json.dumps({
                "api_origin": self.endpoint.api_origin, "provider_ref": self.endpoint.provider_ref,
                "items": [], "capacity": {"provider_items": 8, "provider_bytes": 4096},
                "publisher": {"state": "running"},
            }).encode()
            writer.write(f"HTTP/1.1 {self.status} Reply\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.handlers.discard(task)

    async def close_peer(self):
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        if self.handlers:
            await asyncio.gather(*tuple(self.handlers))

    async def test_missing_relay_recovers_inside_original_whole_read_operation(self):
        started = None
        failures, completed_views = [], []

        def retry(kind, failure, _seconds):
            nonlocal started
            self.assertEqual(kind, "delivery")
            self.assertTrue(self.client.retirement_confirmed)
            self.assertIs(failure, self.client.last_failure)
            failures.append(failure)
            if started is None:
                started = asyncio.create_task(self.start_peer())

        async def complete_view():
            relay = await self.client.pending()
            completed_views.append(relay)
            return "fresh complete view"

        self.service._on_api_retry = retry
        try:
            result = await asyncio.wait_for(self.service._read_until_available(complete_view), 2)
        finally:
            if started is not None:
                await started
        self.assertEqual(result, "fresh complete view")
        self.assertEqual(len(completed_views), 1)
        self.assertTrue(failures)
        self.service._on_api_recovered.assert_called_once_with("delivery", failures[-1])
        self.service.api.retire.assert_not_awaited()
        self.service.api.can_retry_read.assert_not_called()
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0].startswith(b"GET /pending HTTP/1.1\r\n"))

    async def test_stop_ends_idle_backoff_without_another_read_or_mutation(self):
        reads = 0

        async def complete_view():
            nonlocal reads
            reads += 1
            return await self.client.pending()

        self.service._on_api_retry = lambda *_: self.service._stop.set()
        self.assertIsNone(await asyncio.wait_for(self.service._read_until_available(complete_view), 1))
        self.assertEqual(reads, 1)
        self.assertTrue(self.client.retirement_confirmed)
        self.assertEqual(self.requests, [])
        self.service._on_api_recovered.assert_not_called()

    async def test_failed_original_retirement_grants_no_read_retry(self):
        failure = DeliveryUnavailable("Status unavailable", status=503)
        cleanup = ExchangeFailed("Original IPC closure remains held")
        delivery = SimpleNamespace(pending=AsyncMock(side_effect=failure), retire=AsyncMock(side_effect=cleanup), can_retry=Mock())
        self.service.delivery = delivery
        with self.assertRaises(ExchangeFailed) as caught:
            await self.service._read_until_available(delivery.pending)
        self.assertIs(caught.exception, cleanup)
        self.assertIs(self.service.last_failure.cause, failure)
        delivery.pending.assert_awaited_once()
        delivery.retire.assert_awaited_once()
        delivery.can_retry.assert_not_called()
        self.service._on_api_retry.assert_not_called()

    async def test_permanent_protocol_refusal_does_not_enter_availability_retry(self):
        self.status = 400
        await self.start_peer()
        with self.assertRaises(DeliveryRejected):
            await self.service._read_until_available(self.client.pending)
        self.assertEqual(len(self.requests), 1)
        self.service._on_api_retry.assert_not_called()
        self.service.api.retire.assert_not_awaited()
