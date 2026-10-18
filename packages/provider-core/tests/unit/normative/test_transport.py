"""A response crosses the transport boundary only after owned resources close."""

import ssl
import unittest
from unittest.mock import patch

import httpx

from provider_core.transport import HttpTransport
from tests.fakes.transport import Client, Stream


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_closes_before_handoff_and_next_call_gets_fresh_client(self):
        clients = [Client(Stream([b"ab", b"cd"])), Client(Stream([b"ok"]))]
        trust = ssl.create_default_context()
        transport = HttpTransport(trust=trust, exchange_seconds=1, retirement_seconds=1)
        with patch("provider_core.transport.httpx.AsyncClient", side_effect=clients) as factory:
            response = await transport.exchange(httpx.Request("GET", "https://api.example/input"), maximum_bytes=4)
            self.assertEqual(response.body, b"abcd")
            self.assertEqual(response.status, 200)
            self.assertTrue(clients[0].closed)
            self.assertTrue(clients[0].stream.closed)
            self.assertEqual(clients[0].calls[0][1], {"auth": None, "follow_redirects": False, "stream": True})
            await transport.exchange(httpx.Request("GET", "https://api.example/input"), maximum_bytes=4)
        self.assertEqual(factory.call_count, 2)
        self.assertIs(factory.call_args.kwargs["verify"], trust)
        self.assertFalse(factory.call_args.kwargs["trust_env"])
        self.assertTrue(clients[1].closed)
