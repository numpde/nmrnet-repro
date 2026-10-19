"""Chat failures preserve private causes without retries or leaked model text."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.fakes.tls_peer import TlsPeer

from nmrnet_provider.chat import ChatEndpoint, ChatEndpointSpec, ChatFailed
from provider_core.transport import ExchangeFailed, HttpResponse, HttpTransport


class ChatCarrierRejectionTests(unittest.IsolatedAsyncioTestCase):
    def spec(self, url):
        return ChatEndpointSpec(
            configuration_id="local-test", model="fixture-model", url=url, api_key="PRIVATE_KEY", reasoning_effort=None
        )

    async def test_oversized_request_is_rejected_before_http(self):
        transport = SimpleNamespace(exchange=AsyncMock(), retire=AsyncMock(), retirement_confirmed=True)
        endpoint = ChatEndpoint(
            self.spec("https://example.invalid/chat"), transport=transport, request_bytes=64, response_bytes=4096
        )
        with self.assertRaisesRegex(ChatFailed, "request.*byte limit"):
            await endpoint.complete([{"role": "user", "content": "PRIVATE_SOURCE" * 100}], [])
        transport.exchange.assert_not_awaited()

    async def test_http_failure_and_malformed_replies_keep_raw_evidence_private(self):
        for status, body in (
            (401, b"PRIVATE_KEY PRIVATE_SOURCE"),
            (200, b'{"choices":[],"choices":[]}'),
            (200, b'{"choices": []}'),
            (200, b"PRIVATE_SOURCE invalid json"),
            (200, json.dumps({"choices": [{"message": {"role": "assistant"}, "finish_reason": "length"}]}).encode()),
            (200, json.dumps({"choices": [{"message": {"role": "assistant"}, "finish_reason": None}]}).encode()),
            (200, json.dumps({"choices": [{"message": {"role": "assistant"}, "finish_reason": "content_filter"}]}).encode()),
        ):
            with self.subTest(status=status, body=body):
                response = HttpResponse(status, ((b"content-type", b"application/json"),), body)
                transport = SimpleNamespace(
                    exchange=AsyncMock(return_value=response), retire=AsyncMock(), retirement_confirmed=True
                )
                endpoint = ChatEndpoint(
                    self.spec("https://example.invalid/chat"), transport=transport, request_bytes=4096, response_bytes=4096
                )
                with self.assertRaises(ChatFailed) as caught:
                    await endpoint.complete([], [])
                transport.exchange.assert_awaited_once()
                self.assertIs(caught.exception.response, response)
                self.assertNotIn("PRIVATE", str(caught.exception))

    async def test_consumed_response_limit_and_trickle_deadline_retire_actual_connections(self):
        for mode, maximum in (("success", 16), ("trickle", 4096)):
            with self.subTest(mode=mode):
                async with TlsPeer(mode, response_body=b"x" * 100) as peer:
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=0.15, retirement_seconds=0.5)
                    endpoint = ChatEndpoint(
                        self.spec(peer.origin + "/chat"), transport=transport, request_bytes=4096, response_bytes=maximum
                    )
                    self.addAsyncCleanup(endpoint.retire)
                    with self.assertRaises(ChatFailed) as caught:
                        await asyncio.wait_for(endpoint.complete([], []), 2)
                    self.assertIsInstance(caught.exception.cause, ExchangeFailed)
                    self.assertTrue(endpoint.retirement_confirmed)
                    self.assertEqual(len(peer.requests), 1)
                    await asyncio.wait_for(peer.closed.wait(), 1)

    async def test_cancelled_exchange_is_retired_and_remains_cancellation(self):
        async with TlsPeer("headers-stall") as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            endpoint = ChatEndpoint(
                self.spec(peer.origin + "/chat"), transport=transport, request_bytes=4096, response_bytes=4096
            )
            self.addAsyncCleanup(endpoint.retire)
            task = asyncio.create_task(endpoint.complete([], []))
            await asyncio.wait_for(peer.received.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(endpoint.retirement_confirmed)
            await asyncio.wait_for(peer.closed.wait(), 1)

    async def test_unconfirmed_retirement_preserves_the_transport_failure(self):
        failure = ExchangeFailed("Resource retirement is unconfirmed", primary=OSError("PRIVATE"))
        transport = SimpleNamespace(
            exchange=AsyncMock(side_effect=failure), retire=AsyncMock(side_effect=failure), retirement_confirmed=False
        )
        endpoint = ChatEndpoint(
            self.spec("https://example.invalid/chat"), transport=transport, request_bytes=4096, response_bytes=4096
        )
        with self.assertRaises(ChatFailed) as caught:
            await endpoint.complete([], [])
        self.assertIs(caught.exception.cause, failure)
        self.assertFalse(endpoint.retirement_confirmed)
        with self.assertRaises(ChatFailed) as retired:
            await endpoint.retire()
        self.assertIs(retired.exception.cause, failure)

    async def test_lost_reply_does_not_repeat_the_potentially_billed_post(self):
        async def lose_reply(_request, _body):
            return None

        async with TlsPeer("success", response_factory=lose_reply) as peer:
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            endpoint = ChatEndpoint(
                self.spec(peer.origin + "/chat"), transport=transport, request_bytes=4096, response_bytes=4096
            )
            self.addAsyncCleanup(endpoint.retire)
            with self.assertRaises(ChatFailed):
                await endpoint.complete([], [])
            self.assertEqual(len(peer.requests), 1)
            self.assertTrue(endpoint.retirement_confirmed)

    def test_non_https_credentials_queries_and_fragments_are_not_endpoint_authority(self):
        for url in (
            "http://example.invalid/chat",
            "https://user:secret@example.invalid/chat",
            "https://example.invalid/chat?secret=x",
            "https://example.invalid/chat#fragment",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.spec(url)
