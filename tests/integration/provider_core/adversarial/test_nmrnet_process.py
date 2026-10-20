"""Private replies, notes, hostile messages and long native traces stay controlled."""

import asyncio
import json
import signal
import unittest
from builtins import BaseExceptionGroup

from tests.integration.nmrnet.process_ports import ProcessPeers

from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.worker_client import WorkerReportedFailure, WorkerResultRejected
from provider_core.attempt_store import StoreRefused


class ForegroundProcessRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_publication_failure_keeps_safe_operator_cause_after_dependency_release(self):
        async with ProcessPeers() as fixture:
            process = await fixture.start()
            await asyncio.wait_for(fixture.discovered.wait(), 5)
            (fixture.diagnostics / "result.json").write_bytes(b"PRIVATE earlier result")
            process.send_signal(signal.SIGTERM)
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 1)
            self.assertEqual((fixture.diagnostics / "result.json").read_bytes(), b"PRIVATE earlier result")
            self.assertEqual(await fixture.reopen_store(), ())
            report = (fixture.diagnostics / "failure.json").read_bytes()
            for received in (report, stderr):
                self.assertIn(b"process result publication", received)
                self.assertIn(b'"errno":17', received)
                self.assertIn(b'"requires_host_retirement":false', received)
                self.assertNotIn(b"PRIVATE", received)

    async def test_held_work_reports_before_host_kill_and_keeps_its_store_and_original_task(self):
        async with ProcessPeers() as fixture:
            fixture.offer_job = True
            fixture.files.write(
                fixture.files.raw.replace(b"store_seconds = 2", b"store_seconds = 60")
                .replace(b"drain_seconds = 1", b"drain_seconds = 0.05")
                .replace(b"settle_seconds = 5", b"settle_seconds = 0.05")
            )
            process = await fixture.start(held_work=True)
            await fixture.observed("read-entered")
            process.send_signal(signal.SIGTERM)
            await fixture.observed("failure.json")
            report = (fixture.diagnostics / "failure.json").read_bytes()
            self.assertIn(b'"requires_host_retirement":true', report)
            self.assertIn(b"Original service work is still pending", report)
            self.assertIsNone(process.returncode)
            self.assertTrue((fixture.diagnostics / "original-owner-retained").exists())
            with self.assertRaisesRegex(StoreRefused, "lock|owner|owned|open"):
                await fixture.reopen_store()
            # A second normal stop is not a second task cancellation. The marker
            # is published only by the real process's signal callback.
            process.send_signal(signal.SIGTERM)
            await fixture.observed(f"signal-{signal.SIGTERM}-2")
            self.assertTrue((fixture.diagnostics / "first-cancellation").exists())
            for absent in ("factory-exited", "factory-cleanup-entered", "store-close-entered", "result.json"):
                self.assertFalse((fixture.diagnostics / absent).exists(), absent)
            self.assertTrue(all(event in ("execution-attempts", "hello", "jobs") for event in fixture.events))
            process.kill()
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertNotIn(b"PRIVATE", stderr)
            self.assertEqual(await fixture.reopen_store(), ())

    async def test_malformed_or_private_declarations_stop_before_storage_and_network(self):
        for change in (
            "empty",
            "missing",
            "private",
            "image",
            "revision",
            "source",
            "member_private",
            "member_missing",
            "member_digest",
            "member_size",
            "member_path",
            "deep_json",
        ):
            with self.subTest(change=change):
                async with ProcessPeers() as fixture:
                    execution = json.loads(fixture.execution.read_bytes())
                    models = json.loads(fixture.models.read_bytes())
                    member = models["elements"]["H"]["checkpoint"]
                    if change == "empty":
                        execution.clear()
                    elif change == "missing":
                        del execution["image_id"]
                    elif change == "private":
                        execution["PRIVATE_FIELD"] = "PRIVATE_CANARY"
                    elif change == "image":
                        execution["controller_image_id"] = "mutable:tag"
                    elif change == "revision":
                        execution["repository_revision"] = "short"
                    elif change == "source":
                        execution["runtime_identity_source"] = "observed"
                    elif change == "member_private":
                        member["PRIVATE_FIELD"] = "PRIVATE_CANARY"
                    elif change == "member_missing":
                        del member["sha256"]
                    elif change == "member_digest":
                        member["sha256"] = "not a digest"
                    elif change == "member_size":
                        member["size"] = True
                    elif change == "member_path":
                        member["member"] = "../PRIVATE_PATH"
                    fixture.execution.write_text(json.dumps(execution))
                    fixture.models.write_text(json.dumps(models))
                    if change == "deep_json":
                        fixture.execution.write_text("[" * 2000 + "0" + "]" * 2000)
                    process = await fixture.start()
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
                    self.assertEqual(process.returncode, 2, stderr.decode())
                    self.assertEqual(fixture.peer.requests, [])
                    self.assertFalse(fixture.files.state.exists())
                    self.assertFalse((fixture.diagnostics / "admitted").exists())
                    report = (fixture.diagnostics / "failure.json").read_bytes()
                    self.assertIn(b"ConfigurationRejected", report)
                    self.assertNotIn(b"PRIVATE", report + stdout + stderr)

    async def test_unadmitted_diagnostics_retain_safe_reason_without_writing_into_that_directory(self):
        for change in ("missing", "permissions", "stale"):
            with self.subTest(change=change):
                async with ProcessPeers() as fixture:
                    expected = b'"errno":2'
                    if change == "missing":
                        fixture.diagnostics.rmdir()
                    elif change == "permissions":
                        fixture.diagnostics.chmod(0o755)
                        expected = b"mode 0700"
                    else:
                        (fixture.diagnostics / "admitted").write_text("PRIVATE earlier evidence")
                        expected = b"earlier process evidence"
                    process = await fixture.start()
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
                    self.assertEqual(process.returncode, 2)
                    self.assertIn(expected, stderr)
                    self.assertNotIn(b"PRIVATE", stdout + stderr)
                    self.assertFalse((fixture.diagnostics / "failure.json").exists())
                    self.assertFalse(fixture.files.state.exists())
                    self.assertEqual(fixture.peer.requests, [])

    async def test_retained_pending_start_stops_before_hello_without_changing_its_bytes(self):
        async with ProcessPeers() as fixture:
            pending = await fixture.seed_pending()
            before = {path.name: path.read_bytes() for path in fixture.files.state.iterdir() if path.is_file()}
            process = await fixture.start()
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertEqual(process.returncode, 1, stderr.decode())
            self.assertEqual(fixture.events, [])
            self.assertEqual(await fixture.reopen_store(), (pending,))
            self.assertEqual(before, {path.name: path.read_bytes() for path in fixture.files.state.iterdir() if path.is_file()})
            self.assertIn(b"retained Attempt obligations require explicit recovery", stderr)
            result = json.loads((fixture.diagnostics / "result.json").read_bytes())
            self.assertEqual(result, {"state": "stopped_after_failure", "requires_host_retirement": False})

    async def test_invalid_config_has_no_journal_or_requests_and_no_private_echo(self):
        async with ProcessPeers() as fixture:
            fixture.files.write(fixture.files.raw + b'unknown = "PRIVATE_INVALID"\n')
            process = await fixture.start()
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(fixture.peer.requests, [])
            self.assertFalse(fixture.files.state.exists())
            self.assertNotIn(b"PRIVATE", stdout + stderr)
            self.assertTrue((fixture.diagnostics / "failure.json").exists())

    async def test_malformed_api_reply_reaches_operator_with_status_and_request_id_but_no_payload(self):
        async with ProcessPeers() as fixture:
            fixture.bad_reply = True
            process = await fixture.start()
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertNotEqual(process.returncode, 0)
            report = (fixture.diagnostics / "failure.json").read_bytes()
            for received in (report, stderr):
                self.assertIn(b"request:process-proof", received)
                self.assertIn(b'"status":200', received)
                self.assertNotIn(b"PRIVATE", received)
            self.assertEqual(await fixture.reopen_store(), ())
            self.assertEqual(fixture.events, ["execution-attempts"])
            self.assertNotIn(b"PRIVATE", stdout)


class ProcessDiagnosticRefusalTests(unittest.TestCase):
    def test_native_cause_keeps_its_tail_while_credentials_and_raw_results_are_excluded(self):
        secret = 'configured-key"\\tail'
        diagnostic = (
            "Native traceback\n"
            + repr(secret)
            + '\napi_key="prefix\\"ESCAPED_SECRET_SUFFIX"\napi_key=OTHER_SECRET\nBearer REMOTE_SECRET\n'
            + "middle\n" * 3000
            + "ValueError: final-native-cause"
        )
        native = WorkerReportedFailure(diagnostic)
        result = WorkerResultRejected({"PRIVATE_RESULT": "not for operator projection"})
        report = failure_evidence(BaseExceptionGroup("PRIVATE_GROUP", [native, result]), secrets=(secret,))
        rendered = json.dumps(report)
        self.assertIn("Native traceback", rendered)
        self.assertIn("final-native-cause", rendered)
        self.assertIn("omitted_characters", rendered)
        for private in (
            "configured-key",
            "ESCAPED_SECRET_SUFFIX",
            "OTHER_SECRET",
            "REMOTE_SECRET",
            "PRIVATE_RESULT",
            "PRIVATE_GROUP",
        ):
            self.assertNotIn(private, rendered)

    def test_shared_causes_cycles_and_wide_groups_share_one_bound_without_rendering_unknown_messages(self):
        class HostileError(Exception):
            def __str__(self):
                raise AssertionError("Unknown exception text must not be rendered")

        first, second = HostileError(), RuntimeError("PRIVATE_RUNTIME")
        first.__cause__, first.__context__ = second, second
        second.__cause__ = first
        report = failure_evidence(BaseExceptionGroup("PRIVATE_GROUP", [first] * 100), secrets=())
        rendered = json.dumps(report)
        self.assertIn("exception_reference", rendered)
        self.assertIn("omitted", rendered)
        self.assertNotIn("PRIVATE", rendered)
        self.assertLess(len(rendered), 65536)
