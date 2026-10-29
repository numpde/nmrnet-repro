"""Private replies, notes, hostile messages and long native traces stay controlled."""

import asyncio
import errno
import json
import os
import signal
import tempfile
import unittest
from builtins import BaseExceptionGroup
from pathlib import Path
from unittest.mock import patch

from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.process_ports import ProcessPeers, read_process_failure

from nmrnet_provider.process import _publish
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.worker_client import WorkerReportedFailure, WorkerResultRejected


class ForegroundProcessRefusalTests(unittest.IsolatedAsyncioTestCase):
    async def test_sigterm_drains_model_timeout_and_preserves_independent_cleanup_failure(self):
        for cleanup_failure in (False, True):
            with self.subTest(cleanup_failure=cleanup_failure):
                async with (
                    TlsPeer(
                        "body-stall",
                        response_headers=((b"x-request-id", b"request:interrupted-model"), (b"x-private", b"PRIVATE_HEADER")),
                    ) as model,
                    ProcessPeers(model_peer=model) as fixture,
                ):
                    if cleanup_failure:
                        (fixture.diagnostics / "inject-input-cleanup-failure").write_bytes(b"test-only\n")
                    process = await fixture.start(child_script="interrupted_model_process.py")
                    await fixture.observed("model-head-received")
                    process.send_signal(signal.SIGTERM)
                    report = json.loads(await read_process_failure(process))
                    rendered = json.dumps(report)
                    for fact in (
                        "ChatFailed",
                        '"reason": "exchange_failed"',
                        '"status": 200',
                        "request:interrupted-model",
                        "aggregate deadline expired",
                    ):
                        self.assertIn(fact, rendered)
                    self.assertNotIn("PRIVATE", rendered)
                    self.assertIn('"job_ref": "job:example"', rendered)
                    self.assertIn('"execution_attempt_ref": "execution_attempt:sha256:' + "a" * 64 + '"', rendered)
                    self.assertFalse((fixture.diagnostics / "science-entered").exists())
                    self.assertEqual(len(model.requests), 1)
                    self.assertNotIn("complete", fixture.events)
                    self.assertNotIn("fail", fixture.events)
                    if cleanup_failure:
                        self.assertIn('"errno": 5', rendered)
                        self.assertIn('"requires_host_retirement": true', rendered)
                        self.assertIsNone(process.returncode)
                        with self.assertRaises(BlockingIOError):
                            fixture.assert_lifetime_released()
                        process.kill()
                        await asyncio.wait_for(process.communicate(), 5)
                    else:
                        _, stderr = await asyncio.wait_for(process.communicate(), 5)
                        self.assertEqual(process.returncode, 1, stderr.decode())
                        self.assertIn('"requires_host_retirement": false', rendered)
                        self.assertNotIn("PRIVATE", stderr.decode())
                    fixture.assert_lifetime_released()

    async def test_reporting_stream_failure_does_not_release_held_process_or_lifetime(self):
        async with ProcessPeers() as fixture:
            fixture.offer_job = True
            fixture.files.write(
                fixture.files.raw.replace(b"drain_seconds = 1", b"drain_seconds = 0.05")
                .replace(b"settle_seconds = 5", b"settle_seconds = 0.05")
            )
            (fixture.diagnostics / "inject-report-stream-failure").write_bytes(b"test-only\n")
            process = await fixture.start(child_script="held_process.py")
            await fixture.observed("factory-entered")
            process.send_signal(signal.SIGTERM)
            await fixture.observed("report-stream-failed")
            report = (fixture.diagnostics / "report-stream-failed").read_text()
            self.assertIn('"requires_host_retirement": true', report)
            self.assertIn("Original service work is still pending", report)
            self.assertIn('"errno": 32', report)
            self.assertNotIn("PRIVATE", report)
            self.assertIsNone(process.returncode)
            with self.assertRaises(BlockingIOError):
                fixture.assert_lifetime_released()
            for absent in ("factory-exited", "factory-cleanup-entered", "lifetime-close-entered", "result.json"):
                self.assertFalse((fixture.diagnostics / absent).exists(), absent)
            process.kill()
            await asyncio.wait_for(process.communicate(), 5)
            fixture.assert_lifetime_released()

    async def test_missing_or_invalid_process_routes_stop_before_journal_network_or_native_work(self):
        for route in ("missing", "relative", "oversize"):
            with self.subTest(route=route):
                async with ProcessPeers() as fixture:
                    if route == "missing":
                        fixture.proxy_arguments = []
                    elif route == "relative":
                        fixture.proxy_arguments[1] = "PRIVATE_RELATIVE"
                    else:
                        fixture.proxy_arguments[3] = "/" + "x" * 107
                    process = await fixture.start()
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
                    self.assertEqual(process.returncode, 2, stderr.decode())
                    self.assertEqual(fixture.peer.requests, [])
                    self.assertFalse(fixture.files.state.exists())
                    self.assertFalse((fixture.diagnostics / "admitted").exists())
                    self.assertNotIn(b"PRIVATE", stdout + stderr)

    async def test_held_setup_reports_before_host_kill_and_keeps_its_lifetime_and_original_task(self):
        async with ProcessPeers() as fixture:
            fixture.offer_job = True
            fixture.files.write(
                fixture.files.raw.replace(b"drain_seconds = 1", b"drain_seconds = 0.05")
                .replace(b"settle_seconds = 5", b"settle_seconds = 0.05")
            )
            process = await fixture.start(child_script="held_process.py")
            await fixture.observed("factory-entered")
            process.send_signal(signal.SIGTERM)
            report = await read_process_failure(process)
            self.assertIn(b'"requires_host_retirement":true', report)
            self.assertIn(b"Original service work is still pending", report)
            self.assertIsNone(process.returncode)
            self.assertTrue((fixture.diagnostics / "original-owner-retained").exists())
            self.assertTrue((fixture.diagnostics / "cutoff-before-cancellation").exists())
            with self.assertRaises(BlockingIOError):
                fixture.assert_lifetime_released()
            # A second normal stop is not a second task cancellation. The marker
            # is published only by the real process's signal callback.
            process.send_signal(signal.SIGTERM)
            await fixture.observed(f"signal-{signal.SIGTERM}-2")
            self.assertTrue((fixture.diagnostics / "first-cancellation").exists())
            for absent in ("factory-exited", "factory-cleanup-entered", "lifetime-close-entered", "result.json"):
                self.assertFalse((fixture.diagnostics / absent).exists(), absent)
            self.assertTrue(all(event in ("hello", "jobs") for event in fixture.events))
            process.kill()
            _, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertNotIn(b"PRIVATE", stderr)
            fixture.assert_lifetime_released()

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
                    report = stderr
                    self.assertIn(b"ConfigurationRejected", report)
                    self.assertNotIn(b"PRIVATE", report + stdout + stderr)

    async def test_unadmitted_diagnostics_retain_safe_reason_without_writing_into_that_directory(self):
        for change in ("missing", "permissions"):
            with self.subTest(change=change):
                async with ProcessPeers() as fixture:
                    expected = b'"errno":2'
                    if change == "missing":
                        fixture.diagnostics.rmdir()
                    elif change == "permissions":
                        fixture.diagnostics.chmod(0o755)
                        expected = b"mode 0700"
                    process = await fixture.start()
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
                    self.assertEqual(process.returncode, 2)
                    self.assertIn(expected, stderr)
                    self.assertNotIn(b"PRIVATE", stdout + stderr)
                    self.assertFalse((fixture.diagnostics / "failure.json").exists())
                    self.assertFalse(fixture.files.state.exists())
                    self.assertEqual(fixture.peer.requests, [])


    async def test_invalid_config_has_no_journal_or_requests_and_no_private_echo(self):
        async with ProcessPeers() as fixture:
            fixture.files.write(fixture.files.raw + b'unknown = "PRIVATE_INVALID"\n')
            process = await fixture.start()
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(fixture.peer.requests, [])
            self.assertFalse(fixture.files.state.exists())
            self.assertNotIn(b"PRIVATE", stdout + stderr)
            self.assertFalse((fixture.diagnostics / "failure.json").exists())

    async def test_malformed_api_reply_reaches_operator_with_status_and_request_id_but_no_payload(self):
        async with ProcessPeers() as fixture:
            fixture.bad_reply = True
            process = await fixture.start()
            stdout, stderr = await asyncio.wait_for(process.communicate(), 5)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b"request:process-proof", stderr)
            self.assertIn(b'"status":200', stderr)
            self.assertNotIn(b"PRIVATE", stderr)
            fixture.assert_lifetime_released()
            self.assertEqual(fixture.events, ["hello"])
            self.assertNotIn(b"PRIVATE", stdout)


class ProcessDiagnosticRefusalTests(unittest.TestCase):
    def test_permission_failure_leaves_only_private_empty_staging_evidence(self):
        for mask in (0o000, 0o022, 0o077):
            with self.subTest(umask=oct(mask)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                previous = os.umask(mask)
                try:
                    failure = OSError(errno.EIO, "injected permission-setting failure")
                    with patch("nmrnet_provider.process.os.fchmod", side_effect=failure), self.assertRaises(OSError) as caught:
                        _publish(root, "execution.json", b"execution declaration")
                finally:
                    os.umask(previous)
                self.assertIs(caught.exception, failure)
                self.assertFalse((root / "execution.json").exists())
                pending = root / ".execution.json.pending"
                self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
                self.assertEqual(pending.read_bytes(), b"")

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
