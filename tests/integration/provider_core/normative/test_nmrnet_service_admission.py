"""Complete signed observations select one exact start for the existing controller."""

import json
import unittest
from dataclasses import asdict
from unittest.mock import patch

from tests.integration.nmrnet.admission_ports import AdmissionPeers, feed_response
from tests.integration.nmrnet.smiles_ports import SessionPorts
from tests.fakes.api_tls import verify_signed_request

from nmrnet_provider.admission import AdmissionPolicy, require_admission_ready, select_pending_start
from nmrnet_provider.runtime import assemble_attempt
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint, checkpoint_start


class ServiceAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_capacity_precedes_fresh_selected_start_without_remote_attempt_traversal(self):
        ports = SessionPorts()
        policy = AdmissionPolicy(page_size=2, maximum_pages=3)
        async with AdmissionPeers(ports) as fixture:
            first = {
                **asdict(ports.instructions.identity),
                "analysis_kind_ref": "peaks_from_mol",
                "created_at": "2026-10-18T00:00:00Z",
            }
            second = first | {"job_ref": "job:second"}
            fixture.feed_replies = [feed_response(jobs=[first, second], next_cursor=None)]
            await require_admission_ready(fixture.config, fixture.api, delivery=fixture.delivery, policy=policy, owner=self)
            pending = await select_pending_start(fixture.config, fixture.api, policy=policy, owner=self, excluded_jobs=frozenset(), claims=fixture.claims)
            self.assertEqual(pending.input, ports.instructions.identity)
            self.assertEqual(pending.scope, fixture.config.lifecycle.scope)
            body = json.loads(pending.body)
            self.assertEqual(body["job_ref"], ports.instructions.identity.job_ref)
            self.assertRegex(body["provider_attempt_key"], r"^[0-9a-f]{32}$")
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
            self.assertEqual(ports.worker.events, [])
            self.assertEqual(fixture.request_paths, ["/provider/v1/jobs"])
            for request, raw in zip(fixture.api_peer.requests, fixture.api_peer.request_bodies, strict=True):
                self.assertEqual(raw, b"")
                self.assertNotIn(b"authorization:", request.lower())
                self.assertNotIn(b"PRIVATE_MODEL_KEY", request)
                verify_signed_request(request, None, origin=fixture.api_peer.origin, key=fixture.config.api.identity.private_key)
            controller = await assemble_attempt(**fixture.arguments(pending))
            fixture.stack.push_async_callback(fixture.settle_controller, controller)
            outcome = await controller.run(pending)
            self.assertIsInstance(outcome, DeliveryAcceptedCheckpoint)
            await fixture.wait_published()
            self.assertEqual(checkpoint_start(outcome), pending)
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
            self.assertTrue(ports.host.confirmed)
            self.assertEqual(ports.worker.events.count("execute"), 1)

    async def test_selection_uses_first_server_row_even_when_not_lexically_first(self):
        ports = SessionPorts()
        async with AdmissionPeers(ports) as fixture:
            row = {
                **asdict(ports.instructions.identity),
                "analysis_kind_ref": "peaks_from_mol",
                "created_at": "2026-10-18T00:00:00Z",
            }
            fixture.feed_replies = [
                feed_response(jobs=[], next_cursor="YQ"),
                feed_response(
                    jobs=[row | {"job_ref": "job:z"}, row | {"job_ref": "job:a"}],
                    next_cursor="Yg",
                ),
            ]
            selected = await select_pending_start(fixture.config, fixture.api, policy=AdmissionPolicy(2, 3), owner=self, excluded_jobs=frozenset(), claims=fixture.claims)
            self.assertEqual(selected.input.job_ref, "job:z")
            self.assertEqual(json.loads(selected.body)["job_ref"], "job:z")
            self.assertEqual(len(fixture.api_peer.requests), 2)
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
            self.assertEqual(ports.worker.events, [])
            await ports.session.retire()

    async def test_complete_empty_feed_is_idle_and_each_selection_starts_a_fresh_walk(self):
        ports = SessionPorts()
        policy = AdmissionPolicy(page_size=2, maximum_pages=3)
        async with AdmissionPeers(ports) as fixture:
            fixture.feed_replies = [
                feed_response(analysis_kind_ref="peaks_from_mol", jobs=[], next_cursor="YQ"),
                feed_response(analysis_kind_ref="peaks_from_mol", jobs=[], next_cursor=None),
            ]
            with patch(
                "nmrnet_provider.admission.secrets.token_hex",
                side_effect=AssertionError("Empty discovery must create no start key"),
            ):
                self.assertIsNone(await select_pending_start(fixture.config, fixture.api, policy=policy, owner=self, excluded_jobs=frozenset(), claims=fixture.claims))
            fixture.feed_replies = None
            selected = await select_pending_start(fixture.config, fixture.api, policy=policy, owner=self, excluded_jobs=frozenset(), claims=fixture.claims)
            self.assertEqual(selected.input, ports.instructions.identity)
            self.assertNotIn(b"cursor=", fixture.api_peer.requests[2])
            self.assertFalse((fixture.files.state / "checkpoints.sqlite3").exists())
            self.assertEqual(ports.worker.events, [])
            await ports.session.retire()
