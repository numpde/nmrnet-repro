"""Unresolved or incomplete observations cannot grant fresh service admission."""

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, PropertyMock, patch

from tests.fakes.attempt_inventory import attempt
from tests.fakes.attempt_inventory import response as inventory_response
from tests.integration.nmrnet.admission_ports import AdmissionPeers, feed_response
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.admission import AdmissionHeld, AdmissionPolicy, require_admission_ready, select_pending_start
from provider_core.api import ApiFailure, ProviderAPI
from provider_core.delivery_store import DeliveryFull
from tests.fakes.delivery import outcome


class ServiceAdmissionRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_retained_control_and_full_real_relay_refuse_before_api_reads(self):
        for reason in ("local", "capacity"):
            with self.subTest(reason=reason):
                ports = SessionPorts()
                async with AdmissionPeers(ports) as fixture:
                    before_requests = ()
                    if reason == "local":
                        pending = await fixture.discover()
                        await fixture.store.retain_start(pending)
                        before = await fixture.store.inventory()
                        before_requests = tuple(fixture.api_peer.requests)
                        with self.assertRaisesRegex(AdmissionHeld, "Retained"):
                            await require_admission_ready(fixture.config, fixture.api, fixture.store,
                                delivery=fixture.delivery, policy=AdmissionPolicy(1, 2), owner=self)
                        self.assertEqual(await fixture.store.inventory(), before)
                    else:
                        # Use the actual queue's serialized owner. Held requests
                        # consume real capacity; no worker notification or science is issued.
                        queue = fixture.relay.inbox.queue
                        maximum = queue.limits.provider_items

                        def retain_held(item):
                            # Keep the real queue's reentrant lock across both
                            # transactions; a worker cannot observe the pending intermediate row.
                            with queue._serial:
                                queue.enqueue(item)
                                queue.hold(item, "Authored capacity fixture")

                        for index in range(maximum):
                            item = replace(outcome(provider=fixture.config.lifecycle.scope.provider_ref, suffix=format(index, "x")),
                                           api_origin=fixture.api.origin)
                            await fixture.relay.inbox.storage(retain_held, item)
                        with self.assertRaises(DeliveryFull):
                            await require_admission_ready(fixture.config, fixture.api, fixture.store,
                                delivery=fixture.delivery, policy=AdmissionPolicy(1, 2), owner=self)
                        self.assertEqual(len((await fixture.delivery.pending())["items"]), maximum)
                    self.assertEqual(tuple(fixture.api_peer.requests), before_requests)
                    self.assertEqual(fixture.model_peer.requests, [])
                    self.assertEqual(ports.worker.events, [])
                    await ports.session.retire()

    async def test_any_remote_analysis_kind_holds_before_discovery(self):
        for kind in ("peaks_from_mol", "unrelated_analysis"):
            ports = SessionPorts()
            async with AdmissionPeers(ports) as fixture:
                fixture.inventory_replies = [inventory_response(attempts=[attempt(analysis_kind_ref=kind)], next_cursor="YQ")]
                with self.assertRaisesRegex(AdmissionHeld, "in-progress") as caught:
                    await require_admission_ready(
                        fixture.config, fixture.api, fixture.store, delivery=fixture.delivery, policy=AdmissionPolicy(1, 2), owner=self
                    )
                self.assertEqual(caught.exception.remote_attempts[0].analysis_kind_ref, kind)
                self.assertEqual(fixture.request_paths, ["/provider/v1/execution-attempts"])
                self.assertEqual(await fixture.store.inventory(), ())
                self.assertEqual(ports.worker.events, [])
                await ports.session.retire()

    async def test_cycle_exhaustion_and_failed_second_page_never_mean_empty(self):
        for route in ("inventory", "feed"):
            for failure in ("cycle", "bound", "bad_body"):
                with self.subTest(route=route, failure=failure):
                    ports = SessionPorts()
                    async with AdmissionPeers(ports) as fixture:
                        response = inventory_response if route == "inventory" else feed_response
                        rows = {"attempts": []} if route == "inventory" else {"jobs": [], "analysis_kind_ref": "peaks_from_mol"}
                        replies = [response(**rows, next_cursor="YQ")]
                        if failure == "bad_body":
                            replies.append(b"PRIVATE malformed second page")
                        else:
                            replies.append(response(**rows, next_cursor="YQ" if failure == "cycle" else "Yg"))
                        # A cycle must fail before the independent page limit.
                        policy = AdmissionPolicy(1, 3 if failure == "cycle" else 2)
                        if route == "inventory":
                            fixture.inventory_replies = replies
                            operation = require_admission_ready(
                                fixture.config, fixture.api, fixture.store, delivery=fixture.delivery, policy=policy, owner=self
                            )
                        else:
                            fixture.feed_replies = replies
                            operation = select_pending_start(fixture.config, fixture.api, policy=policy, owner=self, excluded_jobs=frozenset())
                        expected = ApiFailure if failure == "bad_body" else AdmissionHeld
                        with self.assertRaises(expected) as caught:
                            await operation
                        if failure == "cycle":
                            self.assertIn("repeated a page cursor", str(caught.exception))
                        self.assertNotIn("PRIVATE", str(caught.exception))
                        self.assertEqual(len(fixture.api_peer.requests), 2)
                        self.assertEqual(await fixture.store.inventory(), ())
                        self.assertEqual(ports.worker.events, [])
                        await ports.session.retire()

    async def test_wrong_topology_refuses_before_store_inventory_or_network(self):
        ports = SessionPorts()
        async with AdmissionPeers(ports) as fixture:
            foreign = ProviderAPI(fixture.config.api.identity, fixture.config.api.http.new_transport(), expected_topology="web")
            try:
                with patch.object(fixture.store, "inventory", new=AsyncMock()) as reads, self.assertRaises(ValueError):
                    await require_admission_ready(
                        fixture.config, foreign, fixture.store, delivery=fixture.delivery, policy=AdmissionPolicy(1, 2), owner=self
                    )
                reads.assert_not_awaited()
                self.assertEqual(fixture.api_peer.requests, [])
                self.assertEqual(ports.worker.events, [])
            finally:
                await foreign.retire()
                await ports.session.retire()

    async def test_identity_scope_and_missing_owner_refuse_before_store_or_network(self):
        ports = SessionPorts()
        async with AdmissionPeers(ports) as fixture:
            foreign = ProviderAPI(
                replace(fixture.config.api.identity), fixture.config.api.http.new_transport(), expected_topology="dev-local"
            )
            try:
                with patch.object(fixture.store, "inventory", new=AsyncMock()) as reads:
                    with self.assertRaisesRegex(ValueError, "signing identity"):
                        await require_admission_ready(
                            fixture.config, foreign, fixture.store, delivery=fixture.delivery, policy=AdmissionPolicy(1, 2), owner=self
                        )
                    scope = replace(fixture.store.scope, generation_id="other-generation")
                    with (
                        patch.object(type(fixture.store), "scope", new_callable=PropertyMock, return_value=scope),
                        self.assertRaisesRegex(ValueError, "scope"),
                    ):
                        await require_admission_ready(
                            fixture.config, fixture.api, fixture.store, delivery=fixture.delivery, policy=AdmissionPolicy(1, 2), owner=self
                        )
                    for policy, owner in ((AdmissionPolicy(1, 2), None), (object(), self)):
                        with self.assertRaises(ValueError):
                            await require_admission_ready(fixture.config, fixture.api, fixture.store, delivery=fixture.delivery, policy=policy, owner=owner)
                    reads.assert_not_awaited()
                self.assertEqual(fixture.api_peer.requests, [])
                self.assertEqual(ports.worker.events, [])
            finally:
                await foreign.retire()
                await ports.session.retire()

    def test_invalid_page_work_bounds_are_refused(self):
        for size, pages in ((0, 1), (101, 1), (True, 1), (1, 0), (1, True), (1, 1.5)):
            with self.subTest(size=size, pages=pages), self.assertRaises(ValueError):
                AdmissionPolicy(size, pages)

    async def test_cancelled_inventory_preserves_failure_evidence_without_retry(self):
        ports = SessionPorts()
        async with AdmissionPeers(ports) as fixture:
            fixture.api_peer.mode = "headers-stall"
            task = asyncio.create_task(
                require_admission_ready(fixture.config, fixture.api, fixture.store, delivery=fixture.delivery, policy=AdmissionPolicy(1, 2), owner=self)
            )
            await asyncio.wait_for(fixture.api_peer.received.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsInstance(fixture.api.last_failure.cause.primary, asyncio.CancelledError)
            await fixture.api.retire(owner=self)
            self.assertEqual(len(fixture.api_peer.requests), 1)
            self.assertEqual(await fixture.store.inventory(), ())
            self.assertEqual(ports.worker.events, [])
            await ports.session.retire()
