"""Refusal leaves retained obligations and caller allocations untouched."""

import asyncio
import json
import threading
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from tests.integration.nmrnet.interpreter_ports import UploadPorts
from tests.integration.nmrnet.runtime_ports import RuntimePeers
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.runtime import assemble_attempt
from provider_core.api import ProviderAPI
from provider_core.attempt_files import AttemptFiles
from provider_core.attempt_record import AcknowledgedAttempt, LocalExecutionPhase
from provider_core.controller import ControllerFailed, ControllerRefused
from provider_core.signing import SigningIdentity
from provider_core.uploads import UploadFailed, UploadInputRejected


class ConfiguredRuntimeRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_grant_digest_and_budget_failures_never_invite_model_correction(self):
        for failure_kind in ("grant", "digest", "budget"):
            with self.subTest(failure_kind=failure_kind):
                await self.check_failure_kind(failure_kind)

    async def check_failure_kind(self, failure_kind):
        ports = UploadPorts()
        async with RuntimePeers(ports, "upload") as fixture:
            original_api = fixture.api_reply

            async def api_reply(request, body):
                response = await original_api(request, body)
                if failure_kind == "grant" and b"/read-capability " in request:
                    document = json.loads(response)
                    document["content_hash"] = "sha256:" + "f" * 64
                    response = json.dumps(document).encode()
                return response

            fixture.api_peer.response_factory = api_reply
            if failure_kind == "digest":
                source = ports.source
                fixture.store_peer.response_body = bytes([source[0] ^ 1]) + source[1:]
            elif failure_kind == "budget":
                settings = fixture.config.uploads
                fixture.config = replace(fixture.config, uploads=replace(settings, limits=replace(settings.limits, requests=1)))
            pending = await fixture.discover()
            controller = await assemble_attempt(**fixture.arguments(pending))
            fixture.stack.push_async_callback(controller.settle)
            with self.assertRaises(ControllerFailed):
                await controller.run(pending)
            owner = controller.session._input_preparation
            self.assertIsInstance(owner.source_failure, UploadFailed)
            self.assertNotIsInstance(owner.source_failure, UploadInputRejected)
            self.assertEqual(owner.source_failure.grant_issued, failure_kind != "budget")
            self.assertEqual(len(fixture.model_peer.requests), 2)
            self.assertFalse(any(b"/fail " in request or b"/complete " in request for request in fixture.api_peer.requests))
            self.assertIsNone(fixture.result)
            ports.allocate_reader.assert_not_awaited()
            ports.allocate_generator.assert_not_awaited()
            self.assertEqual(ports.worker.events, [])
            fixture.allocate_prediction.assert_not_awaited()

    async def test_same_signing_identity_with_wrong_topology_refuses_before_inventory_or_adoption(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            foreign = ProviderAPI(fixture.config.api.identity, fixture.config.api.http.new_transport(), expected_topology="web")
            try:
                with (
                    patch.object(fixture.store, "records", new=AsyncMock()) as reads,
                    self.assertRaisesRegex(ValueError, "topology"),
                ):
                    await assemble_attempt(**(fixture.arguments(pending) | {"api": foreign}))
                reads.assert_not_awaited()
                self.assertEqual(fixture.model_peer.requests, [])
                self.assertEqual(len(fixture.api_peer.requests), 1)
                self.assertEqual(ports.worker.events, [])
                self.assertFalse(ports.host.confirmed)
            finally:
                await foreign.retire()
                await ports.session.retire()

    async def test_construction_failure_does_not_acquire_prediction(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            primary = RuntimeError("PRIVATE fixture construction failure")
            with patch("nmrnet_provider.runtime.NMRNetSession", side_effect=primary), self.assertRaises(RuntimeError) as caught:
                await assemble_attempt(**fixture.arguments(pending))
            self.assertIs(caught.exception, primary)
            self.assertEqual(await fixture.store.records(), ())
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertEqual(len(fixture.api_peer.requests), 1)
            self.assertEqual(ports.worker.events, [])
            self.assertFalse(ports.host.confirmed)
            await ports.session.retire()
            self.assertFalse(ports.host.confirmed)
            fixture.allocate_prediction.assert_not_awaited()

    async def test_unconfirmed_scientific_retirement_blocks_publication_without_repeating_science(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            controller = await assemble_attempt(**fixture.arguments(pending))
            fixture.stack.push_async_callback(controller.settle)
            cleanup = OSError("PRIVATE fixture host cleanup failure")
            ports.host.failure = cleanup
            try:
                with self.assertRaises(ControllerFailed) as caught:
                    await controller.run(pending)
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(controller.session.last_retirement_failure.failures, (("native_retirement", cleanup),))
                self.assertFalse(ports.host.confirmed)
                self.assertIsNone(fixture.result)
            finally:
                ports.host.failure = None
            await controller.settle()
            self.assertTrue(ports.host.confirmed)
            self.assertEqual(ports.worker.events.count("execute"), 1)
            self.assertIsNone(fixture.result)

    async def test_cancelled_model_exchange_never_acquires_prediction(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            controller = await assemble_attempt(**fixture.arguments(pending))
            fixture.stack.push_async_callback(controller.settle)
            fixture.model_peer.mode = "headers-stall"
            task = asyncio.create_task(controller.run(pending))
            await asyncio.wait_for(fixture.model_peer.received.wait(), 1)
            task.cancel("fixture interpretation cancellation")
            with self.assertRaises(asyncio.CancelledError):
                await task
            await controller.settle()
            self.assertFalse(ports.host.confirmed)
            self.assertEqual(ports.worker.events, [])
            fixture.allocate_prediction.assert_not_awaited()
            self.assertIsNone(fixture.result)
            ports.allocate_generator.assert_not_awaited()
            ports.allocate_reader.assert_not_awaited()

    async def test_input_changed_after_discovery_blocks_interpreter_and_native_dispatch(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            controller = await assemble_attempt(**fixture.arguments(pending))
            fixture.stack.push_async_callback(controller.settle)
            fixture.corrupt_input = True
            with self.assertRaises(ControllerFailed):
                await controller.run(pending)
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertEqual(ports.worker.events, [])
            self.assertFalse(ports.host.confirmed)
            fixture.allocate_prediction.assert_not_awaited()
            ports.allocate_generator.assert_not_awaited()
            ports.allocate_reader.assert_not_awaited()
            (retained,) = await fixture.store.records()
            self.assertIsInstance(retained, AcknowledgedAttempt)
            self.assertIs(retained.local_phase, LocalExecutionPhase.NOT_ENTERED)
            self.assertIsNone(fixture.result)

    async def test_cancelled_preflight_retains_the_original_inventory_io_for_caller_settlement(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            entered, release = threading.Event(), threading.Event()
            original = AttemptFiles.records

            def delayed_inventory(files):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("Fixture inventory was not released")
                return original(files)

            try:
                with patch.object(AttemptFiles, "records", autospec=True, side_effect=delayed_inventory) as reads:
                    task = asyncio.create_task(assemble_attempt(**fixture.arguments(pending)))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel("fixture preflight cancellation")
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await task
                    self.assertIs(fixture.store.last_failure.cause, caught.exception)
                    self.assertEqual(fixture.store.pending_operation, "read Attempt inventory")
                    self.assertEqual(ports.worker.events, [])
                    self.assertFalse(ports.host.confirmed)
                    ports.allocate_generator.assert_not_awaited()
                    ports.allocate_reader.assert_not_awaited()
                    self.assertEqual(fixture.model_peer.requests, [])
                    self.assertEqual(len(fixture.api_peer.requests), 1)
                    release.set()
                    settled = await fixture.store.settle()
                    self.assertEqual(settled.value, ())
                    reads.assert_called_once()
            finally:
                release.set()
            await ports.session.retire()

    async def test_retained_work_requires_explicit_recovery_before_new_assembly(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            await fixture.store.admit(pending)
            with self.assertRaisesRegex(ControllerRefused, "recovery"):
                await assemble_attempt(**fixture.arguments(pending))
            self.assertEqual(await fixture.store.records(), (pending,))
            self.assertEqual(len(fixture.api_peer.requests), 1)
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertEqual(ports.worker.events, [])
            self.assertFalse(ports.host.confirmed)
            ports.allocate_generator.assert_not_awaited()
            ports.allocate_reader.assert_not_awaited()
            await ports.session.retire()
            self.assertFalse(ports.host.confirmed)
            fixture.allocate_prediction.assert_not_awaited()

    async def test_invalid_context_or_missing_diagnostic_recipient_refuses_before_start(self):
        ports = SessionPorts()
        async with RuntimePeers(ports, "smiles") as fixture:
            pending = await fixture.discover()
            other_identity = SigningIdentity(fixture.api.origin, "credential:other", Ed25519PrivateKey.generate())
            other_api = ProviderAPI(other_identity, fixture.config.api.http.new_transport(), expected_topology="dev-local")
            for changes in (
                {"pending": replace(pending, analysis_kind_ref="nmr_predict")},
                {"pending": replace(pending, scope=replace(pending.scope, generation_id="other"))},
                {"api": other_api},
                {"on_interpreter_failure": None},
            ):
                with self.subTest(changes=tuple(changes)), self.assertRaises(ValueError):
                    await assemble_attempt(**(fixture.arguments(pending) | changes))
            self.assertEqual(await fixture.store.records(), ())
            self.assertEqual(len(fixture.api_peer.requests), 1)
            self.assertEqual(fixture.model_peer.requests, [])
            self.assertEqual(ports.worker.events, [])
            await ports.session.retire()
