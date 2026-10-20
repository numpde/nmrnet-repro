"""A response crosses the transport boundary only after owned resources close."""

import ssl
import unittest
from unittest.mock import patch

import httpx

from provider_core.transport import HttpTransport
from tests.fakes.transport import Client, Stream


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_unix_route_selects_https_proxy_without_creating_a_direct_pool(self):
        client = Client(Stream([b"ok"]))
        trust = ssl.create_default_context()
        transport = HttpTransport(trust=trust, exchange_seconds=1, retirement_seconds=1, proxy_socket="/egress/proxy.sock")
        with (
            patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=AssertionError("direct pool created")),
            patch("provider_core.transport.httpcore.AsyncHTTPProxy", return_value=client) as factory,
        ):
            response = await transport.exchange(httpx.Request("GET", "https://api.example/input"), maximum_bytes=2)
        self.assertEqual(response.body, b"ok")
        self.assertEqual(client.calls[0][0].url.host, b"api.example")
        self.assertIs(factory.call_args.kwargs["ssl_context"], trust)
        self.assertEqual(factory.call_args.kwargs["retries"], 0)
        self.assertTrue(client.closed)

    async def test_success_closes_before_handoff_and_next_call_gets_fresh_client(self):
        clients = [Client(Stream([b"ab", b"cd"])), Client(Stream([b"ok"]))]
        trust = ssl.create_default_context()
        transport = HttpTransport(trust=trust, exchange_seconds=1, retirement_seconds=1)
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", side_effect=clients) as factory:
            response = await transport.exchange(httpx.Request("GET", "https://api.example/input"), maximum_bytes=4)
            self.assertEqual(response.body, b"abcd")
            self.assertEqual(response.status, 200)
            self.assertTrue(clients[0].closed)
            self.assertTrue(clients[0].stream.closed)
            self.assertEqual(clients[0].calls[0][0].url.target, b"/input")
            await transport.exchange(httpx.Request("GET", "https://api.example/input"), maximum_bytes=4)
        self.assertEqual(factory.call_count, 2)
        self.assertIs(factory.call_args.kwargs["ssl_context"], trust)
        self.assertEqual(factory.call_args.kwargs["retries"], 0)
        self.assertTrue(clients[1].closed)
