"""Refused selections never gain network or replacement ownership."""

import asyncio
import json
import signal
import tempfile
import unittest
from unittest.mock import patch

from tests.integration.nmrnet.config_ports import ConfigFiles
from tests.integration.nmrnet.recovery_ports import RecoveryPeers, held_inspection_child, observed_file

from nmrnet_provider.config import ConfigurationRejected, load_recovery_config, load_recovery_inspection
from nmrnet_provider.recovery import recover_record
from provider_core.attempt_store import StoreRefused


class StoppedRecoveryRefusalTests(unittest.IsolatedAsyncioTestCase):
    def test_unrepresentable_store_clock_has_owned_startup_reason(self):
        with tempfile.TemporaryDirectory() as directory:
            files = ConfigFiles(directory)
            files.write(files.raw.replace(b"store_seconds = 2", b"store_seconds = " + b"9" * 400))
            for loader in (load_recovery_inspection, load_recovery_config):
                with self.subTest(loader=loader.__name__), self.assertRaisesRegex(ConfigurationRejected, "finite seconds"):
                    loader(files.path)
            self.assertFalse(files.state.exists())

    async def test_held_storage_timeout_survives_first_signal_and_retains_store_until_exact_kill(self):
        async with RecoveryPeers() as fixture:
            await fixture.retain()
            fixture.files.write(fixture.files.raw.replace(b"store_seconds = 2", b"store_seconds = 0.05"))
            process = await held_inspection_child(fixture.files, fixture.diagnostics)
            try:
                await observed_file(fixture.diagnostics / "failure.json")
                report = (fixture.diagnostics / "failure.json").read_bytes()
                self.assertIn(b'"requires_host_retirement":true', report)
                self.assertIn(b"original storage settlement", report)
                self.assertIsNone(process.returncode)
                process.send_signal(signal.SIGTERM)
                await asyncio.sleep(0.1)
                self.assertIsNone(process.returncode)
                self.assertFalse((fixture.diagnostics / "recovery-main-exited").exists())
                self.assertFalse((fixture.diagnostics / "result.json").exists())
                competing = fixture.store()
                with self.assertRaises(StoreRefused):
                    await competing.open()
                await competing.close()
                self.assertEqual(fixture.peer.requests, [])
                self.assertEqual((fixture.diagnostics / "failure.json").read_bytes(), report)
            finally:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.communicate(), 3)
            self.assertEqual(await fixture.records(), (fixture.record,))

    async def test_cancelled_terminal_exchange_preserves_original_request_and_effect_evidence(self):
        async with RecoveryPeers() as fixture:
            digest = await fixture.retain()
            fixture.peer.mode = "body-stall"
            task = asyncio.create_task(recover_record(fixture.config, record_sha256=digest, diagnostics=fixture.diagnostics))
            await asyncio.wait_for(fixture.peer.received.wait(), 2)
            task.cancel()
            self.assertEqual(await asyncio.wait_for(task, 3), 1)
            self.assertEqual(fixture.peer.request_bodies, [fixture.record.body])
            self.assertEqual(await fixture.records(), (fixture.record,))
            report = (fixture.diagnostics / "failure.json").read_text()
            self.assertIn("attempt_mutation_may_have_committed", report)
            self.assertIn("original", report)
            self.assertNotIn("protected original cause", report)

    async def test_stale_or_malformed_selection_creates_no_transport_and_keeps_record(self):
        for selected in ("a" * 64, "../record", "A" * 64):
            with self.subTest(selected=selected):
                async with RecoveryPeers() as fixture:
                    await fixture.retain()
                    with patch("nmrnet_provider.recovery.ProviderAPI") as api:
                        self.assertEqual(
                            await recover_record(fixture.config, record_sha256=selected, diagnostics=fixture.diagnostics), 1
                        )
                    api.assert_not_called()
                    self.assertEqual(fixture.peer.requests, [])
                    self.assertEqual(await fixture.records(), (fixture.record,))
                    failure = json.loads((fixture.diagnostics / "failure.json").read_bytes())
                    self.assertNotIn("protected original cause", json.dumps(failure))

    async def test_original_store_owner_blocks_recovery_before_network(self):
        async with RecoveryPeers() as fixture:
            digest = await fixture.retain()
            original = fixture.store()
            await original.open()
            try:
                self.assertEqual(await recover_record(fixture.config, record_sha256=digest, diagnostics=fixture.diagnostics), 1)
                self.assertEqual(fixture.peer.requests, [])
                self.assertEqual(await original.records(), (fixture.record,))
            finally:
                await original.close()
