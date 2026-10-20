"""The operator sees safe causal and effect evidence from the actual process owner."""

import asyncio
import errno
import hashlib
import json
import signal
import threading
import unittest
from unittest.mock import patch

from tests.integration.nmrnet.process_ports import ProcessPeers

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import PreparationRetirementFailed
from nmrnet_provider.native_factory import NativeAttemptFactory
from nmrnet_provider.process import _runtime_facts, _settle_borrowers
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.service import ProviderService, ServiceFailed
from nmrnet_provider.worker_protocol import WorkerProtocolError
from provider_core.api import ApiFailure, ProviderAPI
from provider_core.attempt_store import AttemptStore
from provider_core.controller import ControllerFailed
from provider_core.conversation import ConversationRetirementFailed
from provider_core.execution import ExecutionFailed
from provider_core.ownership import AttemptClaims
from provider_core.transport import HttpResponse


class ForegroundProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_factory_owned_storage_can_settle_before_service_releases_its_dependencies(self):
        async with ProcessPeers() as fixture:
            fixture.files.state.mkdir(mode=0o700)
            config = fixture.config
            store = AttemptStore(
                fixture.files.state, scope=config.lifecycle.scope, limits=config.lifecycle.store_limits, operation_seconds=2
            )
            await store.open()
            transport = config.api.http.new_transport()
            api = ProviderAPI(config.api.identity, transport, expected_topology=config.api.expected_topology)
            factory = NativeAttemptFactory(
                config=config,
                api=api,
                store=store,
                claims=AttemptClaims(),
                execution_identity={},
                selected_artifacts={},
                policy=config.service.native,
            )
            service = ProviderService(
                config=config,
                store=store,
                api=api,
                open_attempt=factory,
                current_announcement=lambda: config.service.announcement,
                policy=config.service.policy,
                on_started=lambda: None,
            )
            entered, release = asyncio.Event(), threading.Event()
            loop = asyncio.get_running_loop()
            read = store._files.records

            def blocked_read():
                loop.call_soon_threadsafe(entered.set)
                release.wait()
                return read()

            try:
                with patch.object(store._files, "records", blocked_read):
                    task = asyncio.create_task(store.records(owner=factory))
                    await asyncio.wait_for(entered.wait(), 2)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    with self.assertRaises(ServiceFailed):
                        await service.run(asyncio.Event())
                    self.assertIs(store.pending_owner, factory)
                    release.set()
                    failures = []
                    self.assertTrue(await _settle_borrowers(service, factory, failures))
                    self.assertEqual([operation for operation, _ in failures], ["service settlement"])
                    self.assertIsNone(store.pending_operation)
                    self.assertEqual(fixture.events, [])
            finally:
                release.set()
                if store.pending_operation is not None:
                    await store.settle(owner=factory)
                await service.settle()
                await transport.retire()
                await store.close()

    async def test_declared_execution_and_selected_members_have_an_explicit_public_projection(self):
        async with ProcessPeers() as fixture:
            models = json.loads(fixture.models.read_bytes())
            # Archive acquisition details are not selected-member identities and
            # are deliberately excluded, matching the qualified native result.
            models["dictionary"]["archive"] = {"PRIVATE_ACQUISITION": "PRIVATE_CANARY"}
            models["checkpoint_archive"]["archive"] = {"PRIVATE_ACQUISITION": "PRIVATE_CANARY"}
            fixture.models.write_text(json.dumps(models))
            execution, selected = _runtime_facts(fixture.execution, fixture.models, fixture.config)
            expected = json.loads(fixture.execution.read_bytes())
            expected.update(
                startup_configuration_sha256=fixture.config.source_sha256,
                prediction_declaration_sha256=hashlib.sha256(fixture.models.read_bytes()).hexdigest(),
                execution_declaration_sha256=hashlib.sha256(fixture.execution.read_bytes()).hexdigest(),
            )
            self.assertEqual(execution, expected)
            self.assertEqual(set(selected), {"dictionary", "elements"})
            for member in (
                selected["dictionary"],
                *(member for pair in selected["elements"].values() for member in pair.values()),
            ):
                self.assertEqual(set(member), {"archive_sha256", "member", "size", "sha256"})
            self.assertNotIn("PRIVATE", json.dumps((execution, selected)))
            self.assertEqual(fixture.peer.requests, [])
            self.assertFalse(fixture.files.state.exists())

    async def test_signed_startup_discovery_and_sigterm_release_the_store(self):
        async with ProcessPeers() as fixture:
            process = await fixture.start()
            await asyncio.wait_for(fixture.discovered.wait(), 5)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(fixture.events[:4], ["execution-attempts", "hello", "execution-attempts", "jobs"])
            self.assertEqual(await fixture.reopen_store(), ())
            self.assertEqual(json.loads((fixture.diagnostics / "result.json").read_bytes())["state"], "stopped")
            self.assertNotIn(b"PRIVATE", stdout + stderr)


class ProcessDiagnosticTests(unittest.TestCase):
    def test_reviewed_protocol_and_input_causes_remain_actionable_after_cleanup_failure(self):
        for cause in (
            WorkerProtocolError("Scientific reply belongs to another worker allocation"),
            InputRejected("Cannot read coordinates at line 4: x, y and z must be numbers"),
        ):
            failure = PreparationRetirementFailed((("source", OSError(errno.EIO, "PRIVATE cleanup")),), operation_failure=cause)
            report = json.dumps(failure_evidence(failure, secrets=()))
            self.assertIn(str(cause), report)
            self.assertNotIn("PRIVATE", report)

    def test_interpreter_endpoint_index_and_replaced_interruption_cause_remain_visible(self):
        previous = OSError(errno.ECONNRESET, "PRIVATE prior reason")
        cancelled = asyncio.CancelledError("PRIVATE cancellation")
        conversation = ConversationRetirementFailed(((0, cancelled),), cause=cancelled)
        conversation.interruption_cause = previous
        cancelled.__cause__ = conversation
        preparation = PreparationRetirementFailed((("interpreter", cancelled),), operation_failure=conversation)
        preparation.interruption_cause = previous
        report = json.dumps(failure_evidence(preparation, secrets=()))
        self.assertIn("interpreter endpoint 0 cleanup", report)
        self.assertIn('"errno": 104', report)
        self.assertIn("interruption cause", report)
        self.assertNotIn("PRIVATE", report)

    def test_nested_lifecycle_api_and_independent_cleanup_survive_with_correlation_and_effects(self):
        response = HttpResponse(
            503, ((b"x-request-id", b"request:operator-proof"), (b"x-private", b"PRIVATE_HEADER")), b"PRIVATE_BODY"
        )
        api = ApiFailure("read live Attempt inventory", "API returned HTTP 503", response=response)
        api.attempt_mutation_may_have_committed = True
        api.cause = OSError(errno.ECONNRESET, "PRIVATE transport cause")
        api.add_note("PRIVATE_NOTE")
        primary = ExecutionFailed(cause=api, outcome=None, unconfirmed_record=None, api_failure=api, cleanup_failures=())
        cleanup = OSError(errno.EIO, "PRIVATE_CLEANUP", "/PRIVATE_PATH")
        controller = ControllerFailed(primary, (cleanup,))
        rendered = json.dumps(failure_evidence(ServiceFailed((("Attempt work", controller),)), secrets=()))
        for useful in (
            "Attempt work",
            "ControllerFailed",
            "ExecutionFailed",
            "ApiFailure",
            "request:operator-proof",
            '"status": 503',
            '"attempt_mutation_may_have_committed": true',
            '"errno": 5',
        ):
            self.assertIn(useful, rendered)
        self.assertNotIn("PRIVATE", rendered)
        self.assertIn('"errno": 104', rendered)
