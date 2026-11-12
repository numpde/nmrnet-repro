"""Capacity, exact live Job exclusion and incomplete discovery remain distinct."""

import asyncio
import unittest
from dataclasses import asdict, replace

from tests.integration.nmrnet.admission_ports import AdmissionPeers, feed_response
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.admission import AdmissionIncomplete, AdmissionPolicy, select_pending_start
from provider_core.api import ApiFailure, ProviderAPI


class ServiceAdmissionRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_job_is_excluded_without_a_global_attempt_walk(self):
        ports = SessionPorts()
        async with AdmissionPeers(ports) as fixture:
            pending = await fixture.discover()
            # Exercise the SDK's actual provider-wide index; no START is sent.
            claim = fixture.claims.claim_live(pending, lifecycle_owner=self)
            row = asdict(pending.input) | {"analysis_kind_ref": "peaks_from_mol", "created_at": "2026-10-18T00:00:00Z"}
            fixture.feed_replies = [feed_response(jobs=[row, row | {"job_ref": "job:free"}])]
            selected = await select_pending_start(
                fixture.config, fixture.api, policy=AdmissionPolicy(3, 2), owner=self, claims=fixture.claims
            )
            self.assertEqual("job:free", selected.input.job_ref)
            self.assertEqual(["/provider/v1/jobs", "/provider/v1/jobs"], fixture.request_paths)
            self.assertTrue(claim.active)
            await ports.session.retire()

    async def test_cycle_exhaustion_and_failed_second_page_never_mean_empty(self):
        scenarios = (
            ("cycle", feed_response(next_cursor="YQ"), AdmissionIncomplete, 3, "repeated a page cursor"),
            ("bound", feed_response(next_cursor="Yg"), AdmissionIncomplete, 2, "exceeded its page bound"),
            ("bad_body", b"PRIVATE malformed second page", ApiFailure, 2, None),
        )
        for failure, second_page, expected_exception, page_limit, expected_reason in scenarios:
            with self.subTest(failure=failure):
                ports = SessionPorts()
                async with AdmissionPeers(ports) as fixture:
                    fixture.feed_replies = [feed_response(next_cursor="YQ"), second_page]
                    with self.assertRaises(expected_exception) as caught:
                        await select_pending_start(
                            fixture.config,
                            fixture.api,
                            policy=AdmissionPolicy(1, page_limit),
                            owner=self,
                            claims=fixture.claims,
                        )
                    self.assertNotIn("PRIVATE", str(caught.exception))
                    if expected_reason is not None:
                        self.assertIn(expected_reason, str(caught.exception))
                    self.assertEqual(2, len(fixture.api_peer.requests))
                    self.assertEqual([], ports.worker.events)
                    await ports.session.retire()

    async def test_wrong_identity_topology_or_missing_owner_refuses_before_network(self):
        ports = SessionPorts()
        async with AdmissionPeers(ports) as fixture:
            for identity, topology in ((fixture.config.api.identity, "web"), (replace(fixture.config.api.identity), "dev-local")):
                foreign = ProviderAPI(identity, fixture.config.api.http.new_transport(), expected_topology=topology)
                try:
                    with self.assertRaises(ValueError):
                        await select_pending_start(
                            fixture.config, foreign, policy=AdmissionPolicy(1, 2), owner=self, claims=fixture.claims
                        )
                finally:
                    await foreign.retire()
            for policy, owner in ((AdmissionPolicy(1, 2), None), (object(), self)):
                with self.assertRaises(ValueError):
                    await select_pending_start(fixture.config, fixture.api, policy=policy, owner=owner, claims=fixture.claims)
            self.assertEqual([], fixture.api_peer.requests)
            await ports.session.retire()

    def test_invalid_page_work_bounds_are_refused(self):
        for size, pages in ((0, 1), (101, 1), (True, 1), (1, 0), (1, True), (1, 1.5)):
            with self.subTest(size=size, pages=pages), self.assertRaises(ValueError):
                AdmissionPolicy(size, pages)

    async def test_cancelled_discovery_preserves_the_original_api_failure_without_retry(self):
        ports = SessionPorts()
        async with AdmissionPeers(ports) as fixture:
            fixture.api_peer.mode = "headers-stall"
            task = asyncio.create_task(
                select_pending_start(fixture.config, fixture.api, policy=AdmissionPolicy(1, 2), owner=self, claims=fixture.claims)
            )
            await asyncio.wait_for(fixture.api_peer.received.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsInstance(fixture.api.last_failure.cause.primary, asyncio.CancelledError)
            await fixture.api.retire(owner=self)
            self.assertEqual(1, len(fixture.api_peer.requests))
            await ports.session.retire()
