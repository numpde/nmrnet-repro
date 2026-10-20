"""Finite TLS API/store peers prove credential separation and consumed-work limits."""

import asyncio
import hashlib
import unittest
from dataclasses import replace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from provider_core.api import ProviderAPI
from provider_core.signing import SigningIdentity
from provider_core.transport import HttpTransport
from provider_core.uploads import JobUploads, UploadFailed, UploadLimits
from tests.fakes.acquisition import sources, store_headers
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.uploads import UPLOAD_REF, encoded, grant, member, membership


class UploadAcquisitionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_api_to_bearer_store_returns_exact_verified_bytes(self):
        body = b"geometry"
        fingerprint = "sha256:" + hashlib.sha256(body).hexdigest()
        headers = tuple((key, value) for key, value in store_headers(body) if key != b"content-length")
        async with TlsPeer("success", response_body=body, response_headers=headers) as store_peer:
            upload = member(byte_length=len(body), content_hash=fingerprint)
            document = encoded(membership(uploads=[upload]))
            access = encoded(
                grant(
                    byte_length=len(body),
                    content_hash=fingerprint,
                    download_url=store_peer.origin + "/upload/v1/uploads/" + UPLOAD_REF + "/bytes",
                )
            )
            async with api_tls_peer("success", response_bodies=[document, document, access]) as api_peer:
                api_transport = HttpTransport(trust=api_peer.trust, exchange_seconds=1, retirement_seconds=0.3)
                store_transport = HttpTransport(trust=store_peer.trust, exchange_seconds=1, retirement_seconds=0.3)
                api = ProviderAPI(
                    SigningIdentity(api_peer.origin, "credential:test", Ed25519PrivateKey.generate()),
                    api_transport,
                    expected_topology="dev-local",
                )
                owner = JobUploads(
                    "job:example",
                    api=api,
                    store=store_transport,
                    store_origin=store_peer.origin,
                    limits=UploadLimits(100, 100, 4, 5),
                )
                await owner.inspect()
                verified = await owner.acquire(UPLOAD_REF)
                self.assertEqual(verified.data, body)
                self.assertEqual((owner.received_bytes, owner.requests_started), (8, 4))
                self.assertEqual(len(api_peer.requests), 3)
                self.assertEqual(len(store_peer.requests), 1)
                self.assertTrue(all(b"Signature:" in request for request in api_peer.requests))
                self.assertTrue(all(b"private-bearer-canary" not in request for request in api_peer.requests))
                self.assertIn(b"Authorization: Bearer private-bearer-canary", store_peer.requests[0])
                self.assertNotIn(b"Signature", store_peer.requests[0])
                self.assertNotIn(b"credential:test", store_peer.requests[0])
                await asyncio.wait_for(store_peer.closed.wait(), 1)
                await api_transport.retire()
                await store_transport.retire()

    async def test_deadline_and_cancellation_charge_partial_bytes_and_close_store(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                async with TlsPeer("body-stall") as peer:
                    _, api, _ = sources()
                    api.grant = replace(api.grant, download_url=peer.origin + "/upload/v1/uploads/" + UPLOAD_REF + "/bytes")
                    transport = HttpTransport(trust=peer.trust, exchange_seconds=2, retirement_seconds=0.3)
                    owner = JobUploads(
                        "job:example",
                        api=api,
                        store=transport,
                        store_origin=peer.origin,
                        limits=UploadLimits(100, 100, 10, 1 if cancel else 0.15),
                    )
                    await owner.inspect()
                    acquisition = asyncio.create_task(owner.acquire(UPLOAD_REF))
                    await asyncio.wait_for(peer.received.wait(), 1)
                    if cancel:
                        # Observe actual consumption, rather than guessing from a delay.
                        async with asyncio.timeout(1):
                            while owner.received_bytes == 0:
                                await asyncio.sleep(0.001)
                        acquisition.cancel()
                    with self.assertRaises(asyncio.CancelledError if cancel else UploadFailed):
                        await acquisition
                    self.assertEqual(owner.received_bytes, 1)
                    await asyncio.wait_for(peer.closed.wait(), 1)
                    self.assertTrue(transport.retirement_confirmed)
                    self.assertIsNotNone(owner.last_failure)
                    self.assertTrue(owner.last_failure.grant_issued)
                    self.assertEqual(len(peer.requests), 1)
                    await transport.retire()

    async def test_oversized_received_chunk_spends_budget_before_response_rejection(self):
        async with TlsPeer("success", response_body=b"oversized") as peer:
            _, api, _ = sources()
            api.grant = replace(api.grant, download_url=peer.origin + "/upload/v1/uploads/" + UPLOAD_REF + "/bytes")
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.3)
            owner = JobUploads(
                "job:example", api=api, store=transport, store_origin=peer.origin, limits=UploadLimits(8, 8, 10, 5)
            )
            await owner.inspect()
            with self.assertRaises(UploadFailed) as caught:
                await owner.acquire(UPLOAD_REF)
            self.assertIn("8-byte budget", str(caught.exception))
            self.assertEqual(owner.received_bytes, 9)
            await asyncio.wait_for(peer.closed.wait(), 1)
            self.assertTrue(transport.retirement_confirmed)
            self.assertEqual(len(peer.requests), 1)
