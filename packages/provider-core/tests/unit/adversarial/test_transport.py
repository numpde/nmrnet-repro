"""Failures retain causes and resource ownership without exposing private text."""

import asyncio
import ssl
import traceback
import unittest
from unittest.mock import patch

import httpx

from provider_core.transport import ExchangeFailed, HttpTransport
from tests.fakes.transport import Client, Stream


class TransportFailureTests(unittest.IsolatedAsyncioTestCase):
    def transport(self):
        return HttpTransport(trust=ssl.create_default_context(), exchange_seconds=0.05, retirement_seconds=0.05)

    async def test_overflow_never_hands_off_partial_bytes(self):
        client = Client(Stream([b"abc", b"de"]))
        with (
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
            self.assertRaises(ExchangeFailed) as caught,
        ):
            await self.transport().exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)
        self.assertIn("4", str(caught.exception))
        self.assertTrue(client.closed)
        self.assertTrue(client.stream.closed)

    async def test_primary_and_cleanup_causes_remain_private_and_block_reuse(self):
        primary = httpx.ReadError("private-read-canary")
        cleanup = RuntimeError("private-close-canary")
        client = Client(Stream([], failure=primary), close_failure=cleanup)
        transport = self.transport()
        request = httpx.Request("GET", "https://api.example/private-url-canary")
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client) as factory:
            try:
                await transport.exchange(request, maximum_bytes=4)
            except ExchangeFailed as failure:
                self.assertIs(failure.primary, primary)
                self.assertIn(cleanup, failure.cleanup_failures)
                rendered = "".join(traceback.format_exception(failure)) + repr(failure)
                self.assertNotIn("private-read-canary", rendered)
                self.assertNotIn("private-close-canary", rendered)
                self.assertNotIn("private-url-canary", rendered)
            else:
                self.fail("Failed read/close must not yield a response")
            with self.assertRaises(ExchangeFailed):
                await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)
            self.assertEqual(factory.call_count, 1)

    async def test_deadline_and_cancellation_close_before_reuse(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                client = Client(Stream([]), send_gate=asyncio.Event())
                transport = self.transport()
                with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client):
                    task = asyncio.create_task(transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4))
                    await client.sent.wait()
                    if cancel:
                        task.cancel()
                    with self.assertRaises(asyncio.CancelledError if cancel else ExchangeFailed):
                        await task
                    self.assertTrue(client.closed)
                    await transport.retire()

    async def test_overlap_is_rejected_before_another_client_is_created(self):
        client = Client(Stream([]), send_gate=asyncio.Event())
        transport = self.transport()
        with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client) as factory:
            task = asyncio.create_task(transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4))
            await client.sent.wait()
            with self.assertRaises(ExchangeFailed):
                await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)
            self.assertEqual(factory.call_count, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_read_failure_survives_cleanup_deadline(self):
        primary = httpx.ReadError("private-read-canary")

        class SlowCloseClient(Client):
            async def aclose(self):
                await asyncio.sleep(1)

        transport = self.transport()
        with (
            patch(
                "provider_core.transport.httpcore.AsyncConnectionPool", return_value=SlowCloseClient(Stream([], failure=primary))
            ),
            self.assertRaises(ExchangeFailed) as caught,
        ):
            await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)
        self.assertIs(caught.exception.primary, primary)
        self.assertIsInstance(caught.exception.interruption, TimeoutError)

    async def test_cancellation_resistant_send_stays_owned_across_retirement_attempts(self):
        release = asyncio.Event()

        class ResistantClient(Client):
            async def handle_async_request(self, request, **options):
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
                return await super().handle_async_request(request, **options)

        client = ResistantClient(Stream([b"ok"]))
        transport = self.transport()
        try:
            with patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client) as factory:
                started = asyncio.get_running_loop().time()
                with self.assertRaises(ExchangeFailed) as caught:
                    await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)
                self.assertLess(asyncio.get_running_loop().time() - started, 0.5)
                original = caught.exception
                self.assertIsInstance(original.primary, TimeoutError)
                with self.assertRaises(ExchangeFailed):
                    await transport.retire()
                self.assertIs(transport.last_failure, original)
                with self.assertRaises(ExchangeFailed):
                    await transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4)
                self.assertEqual(factory.call_count, 1)
        finally:
            release.set()
            await transport.retire()
        self.assertTrue(client.closed)
        self.assertIs(transport.last_failure, original)

    async def test_retirement_before_worker_entry_creates_no_client_and_releases_owner(self):
        transport = self.transport()
        with patch("provider_core.transport.httpcore.AsyncConnectionPool") as factory:
            task = asyncio.create_task(transport.exchange(httpx.Request("GET", "https://api.example"), maximum_bytes=4))
            # The exchange is ready first; retirement is ready before its child.
            retirement = asyncio.create_task(transport.retire())
            await retirement
            with self.assertRaises(asyncio.CancelledError):
                await task
            factory.assert_not_called()
            await transport.retire()

    async def test_accounting_observes_chunks_even_when_response_limit_rejects_them(self):
        received = []
        client = Client(Stream([b"abc", b"de"]))
        with (
            patch("provider_core.transport.httpcore.AsyncConnectionPool", return_value=client),
            self.assertRaises(ExchangeFailed),
        ):
            await self.transport().exchange(
                httpx.Request("GET", "https://api.example"), maximum_bytes=4, account_bytes=received.append
            )
        self.assertEqual(received, [3, 2])
        self.assertTrue(client.closed)
