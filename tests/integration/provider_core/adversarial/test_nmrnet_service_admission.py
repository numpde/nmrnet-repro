"""Capacity, exact live Job exclusion and incomplete discovery remain distinct."""

import asyncio
import unittest
from dataclasses import asdict, replace

from tests.fakes.delivery import outcome
from tests.integration.nmrnet.admission_ports import AdmissionPeers, feed_response
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.admission import AdmissionIncomplete, AdmissionPolicy, require_admission_ready, select_pending_start
from provider_core.api import ApiFailure, ProviderAPI
from provider_core.delivery_store import DeliveryFull


class ServiceAdmissionRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_real_relay_refuses_before_api_reads(self):
        ports = SessionPorts()
        async with AdmissionPeers(ports) as fixture:
            queue = fixture.relay.inbox.queue

            def retain_held(item):
                with queue._serial:
                    queue.enqueue(item)
                    queue.hold(item, "Authored capacity fixture")

            for index in range(queue.limits.provider_items):
                item = replace(outcome(provider=fixture.config.lifecycle.scope.provider_ref, suffix=format(index, "x")),
                               api_origin=fixture.api.origin)
                await fixture.relay.inbox.storage(retain_held, item)
            with self.assertRaises(DeliveryFull):
                await require_admission_ready(fixture.config, fixture.api, delivery=fixture.delivery,
                                              policy=AdmissionPolicy(1, 2), owner=self)
            self.assertEqual(fixture.api_peer.requests, [])
            self.assertEqual(ports.worker.events, [])
            await ports.session.retire()

    async def test_live_job_and_queued_job_are_excluded_without_a_global_attempt_walk(self):
        ports = SessionPorts()
        async with AdmissionPeers(ports) as fixture:
            pending = await fixture.discover()
            # Exercise the SDK's actual provider-wide index; no START is sent.
            claim = fixture.claims.claim_live(pending, lifecycle_owner=self)
            row = asdict(pending.input) | {"analysis_kind_ref": "peaks_from_mol", "created_at": "2026-10-18T00:00:00Z"}
            fixture.feed_replies = [feed_response(jobs=[row, row | {"job_ref": "job:queued"}, row | {"job_ref": "job:free"}])]
            selected = await select_pending_start(fixture.config, fixture.api, policy=AdmissionPolicy(3, 2),
                                                 owner=self, excluded_jobs={"job:queued"}, claims=fixture.claims)
            self.assertEqual(selected.input.job_ref, "job:free")
            self.assertEqual(fixture.request_paths, ["/provider/v1/jobs", "/provider/v1/jobs"])
            self.assertTrue(claim.active)
            await ports.session.retire()

    async def test_cycle_exhaustion_and_failed_second_page_never_mean_empty(self):
        for failure in ("cycle", "bound", "bad_body"):
            with self.subTest(failure=failure):
                ports = SessionPorts()
                async with AdmissionPeers(ports) as fixture:
                    fixture.feed_replies = [feed_response(next_cursor="YQ"),
                        b"PRIVATE malformed second page" if failure == "bad_body" else
                        feed_response(next_cursor="YQ" if failure == "cycle" else "Yg")]
                    with self.assertRaises(ApiFailure if failure == "bad_body" else AdmissionIncomplete) as caught:
                        await select_pending_start(fixture.config, fixture.api,
                            policy=AdmissionPolicy(1, 3 if failure == "cycle" else 2), owner=self,
                            excluded_jobs=frozenset(), claims=fixture.claims)
                    self.assertNotIn("PRIVATE", str(caught.exception))
                    self.assertEqual(len(fixture.api_peer.requests), 2)
                    self.assertEqual(ports.worker.events, [])
                    await ports.session.retire()

    async def test_wrong_identity_topology_or_missing_owner_refuses_before_network(self):
        ports = SessionPorts()
        async with AdmissionPeers(ports) as fixture:
            for identity, topology in ((fixture.config.api.identity, "web"),
                                       (replace(fixture.config.api.identity), "dev-local")):
                foreign = ProviderAPI(identity, fixture.config.api.http.new_transport(), expected_topology=topology)
                try:
                    with self.assertRaises(ValueError):
                        await require_admission_ready(fixture.config, foreign, delivery=fixture.delivery,
                                                      policy=AdmissionPolicy(1, 2), owner=self)
                finally:
                    await foreign.retire()
            for policy, owner in ((AdmissionPolicy(1, 2), None), (object(), self)):
                with self.assertRaises(ValueError):
                    await require_admission_ready(fixture.config, fixture.api, delivery=fixture.delivery, policy=policy, owner=owner)
            self.assertEqual(fixture.api_peer.requests, [])
            await ports.session.retire()

    def test_invalid_page_work_bounds_are_refused(self):
        for size, pages in ((0, 1), (101, 1), (True, 1), (1, 0), (1, True), (1, 1.5)):
            with self.subTest(size=size, pages=pages), self.assertRaises(ValueError):
                AdmissionPolicy(size, pages)

    async def test_cancelled_discovery_preserves_the_original_api_failure_without_retry(self):
        ports = SessionPorts()
        async with AdmissionPeers(ports) as fixture:
            fixture.api_peer.mode = "headers-stall"
            task = asyncio.create_task(select_pending_start(fixture.config, fixture.api, policy=AdmissionPolicy(1, 2),
                owner=self, excluded_jobs=frozenset(), claims=fixture.claims))
            await asyncio.wait_for(fixture.api_peer.received.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsInstance(fixture.api.last_failure.cause.primary, asyncio.CancelledError)
            await fixture.api.retire(owner=self)
            self.assertEqual(len(fixture.api_peer.requests), 1)
            await ports.session.retire()
