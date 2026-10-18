"""Raw streams remain owned when TLS upgrade or its cleanup fails."""

import asyncio
import ssl
import unittest
from unittest.mock import patch

from provider_core.network import OwnedNetworkBackend


class RawStream:
    def __init__(self):
        self.close_failure = None
        self.closed = False

    async def start_tls(self, **options):
        raise asyncio.CancelledError

    async def aclose(self):
        if self.close_failure is not None:
            raise self.close_failure
        self.closed = True


class Backend:
    def __init__(self, raw):
        self.raw = raw

    async def connect_tcp(self, **options):
        return self.raw


class NetworkOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_tls_upgrade_keeps_raw_stream_until_close_succeeds(self):
        raw = RawStream()
        with patch("provider_core.network.httpcore.AnyIOBackend", return_value=Backend(raw)):
            backend = OwnedNetworkBackend()
        stream = await backend.connect_tcp("127.0.0.1", 443)
        with self.assertRaises(asyncio.CancelledError):
            await stream.start_tls(ssl.create_default_context(), "localhost")
        failure = RuntimeError("close failed")
        raw.close_failure = failure
        with self.assertRaises(RuntimeError) as caught:
            await backend.aclose()
        self.assertIs(caught.exception, failure)
        self.assertFalse(raw.closed)
        raw.close_failure = None
        await backend.aclose()
        self.assertTrue(raw.closed)
