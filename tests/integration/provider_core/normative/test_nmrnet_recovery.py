"""Stopped recovery retains exact obligations without granting scientific work."""

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.integration.nmrnet.config_ports import ConfigFiles
from tests.integration.nmrnet.recovery_ports import RecoveryPeers, recovery_child

from nmrnet_provider.config import load_recovery_config, load_recovery_inspection, recovery_input_paths
from nmrnet_provider.recovery import inspect_recovery, recover_record
from provider_core.attempt_record import AcknowledgedAttempt, TerminalConfirmed, encode_record


class StoppedRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspection_keeps_owned_hold_correlation_and_suppression_reason_without_payloads(self):
        for phase in ("held", "suppressed"):
            with self.subTest(phase=phase):
                async with RecoveryPeers() as fixture:
                    await fixture.retain(phase)
                    self.assertEqual(await inspect_recovery(fixture.config, diagnostics=fixture.diagnostics), 0)
                    facts = fixture.result()["records"][0]
                    if phase == "held":
                        self.assertEqual(facts["problem_code"], "execution_attempt_failure_replay_mismatch")
                        self.assertEqual(facts["request_id"], "request:held")
                    else:
                        self.assertEqual(facts["suppression_reason"], "caller_cancelled")
                    self.assertFalse(fixture.result()["local_obligations_settled"])
                    self.assertEqual(fixture.peer.requests, [])
                    self.assertEqual(await fixture.records(), (fixture.record,))
                    self.assertNotIn("private result message", json.dumps(fixture.result()))
                    self.assertNotIn("protected original cause", json.dumps(fixture.result()))

    async def test_real_cli_terminal_recovery_uses_the_only_api_unix_route(self):
        async with RecoveryPeers() as fixture:
            digest = await fixture.retain()
            async with recovery_child(fixture, "run", digest) as process:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual((stdout, stderr), (b"", b""))
            self.assertEqual(fixture.peer.request_bodies, [fixture.record.body])
            self.assertTrue(fixture.result()["local_obligations_settled"])
            (confirmed,) = await fixture.records()
            self.assertEqual(confirmed.pending, fixture.record)

    async def test_real_cli_complete_recovery_delivers_original_public_and_private_bytes(self):
        async with RecoveryPeers() as fixture:
            digest = await fixture.retain("complete")
            original_body, original_evidence = fixture.record.body, fixture.record.evidence.content
            async with recovery_child(fixture, "run", digest) as process:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual((stdout, stderr), (b"", b""))
            self.assertEqual(fixture.peer.request_bodies, [original_body])
            self.assertEqual(len(fixture.peer.requests), 1)
            self.assertTrue(fixture.peer.requests[0].startswith(b"POST /provider/v1/execution-attempts/complete "))
            (confirmed,) = await fixture.records()
            self.assertIs(type(confirmed), TerminalConfirmed)
            self.assertEqual(confirmed.pending.body, original_body)
            self.assertEqual(confirmed.pending.evidence.content, original_evidence)
            self.assertTrue(confirmed.receipt.replayed)
            self.assertTrue(fixture.result()["local_obligations_settled"])

    async def test_real_inspect_cli_needs_only_toml_and_retained_store(self):
        async with RecoveryPeers() as fixture:
            await fixture.retain()
            fixture.files.credential.unlink()
            (Path(fixture.peer.scratch.name) / "cert.pem").unlink()
            async with recovery_child(fixture, "inspect") as process:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual((stdout, stderr), (b"", b""))
            self.assertEqual(fixture.peer.requests, [])
            self.assertEqual(await fixture.records(), (fixture.record,))
            self.assertEqual((fixture.diagnostics / "result.json").stat().st_mode & 0o777, 0o600)

    def test_storage_inspection_needs_no_signing_artifact_or_ca(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory, ca_file=Path(directory) / "absent-ca.pem")
            files.key.unlink()
            files.credential.unlink()
            config = load_recovery_inspection(files.path)
            self.assertEqual(config.source_sha256, hashlib.sha256(files.raw).hexdigest())
            self.assertEqual(config.lifecycle.scope.provider_ref, "provider:example")

    def test_api_projection_uses_the_same_toml_without_reading_model_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            files.key.unlink()
            config = load_recovery_config(files.path)
            self.assertEqual(config.source_sha256, hashlib.sha256(files.raw).hexdigest())
            self.assertEqual(config.lifecycle.scope.provider_ref, "provider:example")
            self.assertEqual(recovery_input_paths(files.path), (files.path, files.credential))
            self.assertFalse(files.state.exists())

    async def test_inspection_is_offline_and_contains_only_owned_metadata(self):
        async with RecoveryPeers() as fixture:
            digest = await fixture.retain()
            with patch("nmrnet_provider.recovery.ProviderAPI") as api:
                self.assertEqual(await inspect_recovery(fixture.config, diagnostics=fixture.diagnostics), 0)
            api.assert_not_called()
            self.assertEqual(fixture.peer.requests, [])
            result = fixture.result()
            self.assertEqual(result["records"][0]["record_sha256"], digest)
            self.assertFalse(result["local_obligations_settled"])
            self.assertNotIn("protected original cause", json.dumps(result))
            self.assertNotIn("Explain", json.dumps(result))
            self.assertEqual(await fixture.records(), (fixture.record,))

    async def test_terminal_recovery_sends_exact_retained_body_and_preserves_evidence(self):
        async with RecoveryPeers() as fixture:
            digest = await fixture.retain()
            self.assertEqual(await recover_record(fixture.config, record_sha256=digest, diagnostics=fixture.diagnostics), 0)
            self.assertEqual(fixture.peer.request_bodies, [fixture.record.body])
            (confirmed,) = await fixture.records()
            self.assertIs(type(confirmed), TerminalConfirmed)
            self.assertTrue(confirmed.receipt.replayed)
            self.assertEqual(confirmed.pending, fixture.record)
            self.assertTrue(fixture.result()["local_obligations_settled"])
            self.assertIn("storage capacity", fixture.result()["next_action"])
            self.assertIn("API inventory", fixture.result()["next_action"])
            self.assertEqual(fixture.result()["selected_record_sha256"], digest)

    async def test_start_replays_same_key_but_entered_work_is_only_observed(self):
        for phase in ("start", "entered"):
            with self.subTest(phase=phase):
                async with RecoveryPeers() as fixture:
                    digest = await fixture.retain(phase)
                    before = encode_record(fixture.record)
                    self.assertEqual(
                        await recover_record(fixture.config, record_sha256=digest, diagnostics=fixture.diagnostics), 0
                    )
                    self.assertFalse(fixture.result()["local_obligations_settled"])
                    (retained,) = await fixture.records()
                    self.assertIs(type(retained), AcknowledgedAttempt)
                    if phase == "entered":
                        self.assertEqual(encode_record(retained), before)
                        self.assertTrue(fixture.peer.requests[0].startswith(b"GET "))
                    else:
                        self.assertEqual(fixture.peer.request_bodies, [fixture.record.body])
                    self.assertEqual(len(fixture.peer.requests), 1)
