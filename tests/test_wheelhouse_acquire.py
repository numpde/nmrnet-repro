"""Exercise the shared publisher handoff without downloading artifacts."""

import fcntl
import hashlib
import io
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from tools import wheelhouse_acquire
from tests.unit.wheel_pool_fixtures import locked, wheel


class SharedAcquisitionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "pool"
        self.pool.mkdir()
        self.lock = self.root / "inference.lock"
        self.lock.write_text("torch==2.13.0+cpu \\\n    --hash=sha256:" + "a" * 64 + "\n")
        self.assets = self.root / "assets.in"
        self.assets.write_text("https://github.com/numpde/nmr-provider-tools/releases/download/v1/provider_core-1.0-py3-none-any.whl#sha256=" + "b" * 64 + "\n")
        self.tool_assets = self.root / "tool.in"
        self.tool_assets.write_text("https://github.com/numpde/nmr-provider-tools/releases/download/v1/wheelhouse-tool-1.0.0.pyz#sha256=" + "c" * 64 + "\n")
        self.declaration = self.root / "inference.in"
        self.url = "https://download.pytorch.org/whl/torch-2.13.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl#sha256=" + "a" * 64
        self.declaration.write_text("torch @ " + self.url + "\n")

    def test_warm_pool_needs_no_tool_or_metadata_request(self):
        with (patch.object(wheelhouse_acquire, "prepare_core_assets") as assets,
              patch.object(wheelhouse_acquire, "select_wheels", return_value={}),
              patch.object(wheelhouse_acquire.subprocess, "run") as run):
            wheelhouse_acquire.acquire([self.lock], self.pool, self.assets, self.tool_assets,
                                       declarations=[self.declaration])
        assets.assert_not_called()
        run.assert_not_called()

    def test_shared_tool_failure_preserves_status_without_printing_command(self):
        output = io.StringIO()
        failure = subprocess.CalledProcessError(7, ["tool", "https://secret.invalid/private"])
        with patch.object(wheelhouse_acquire.subprocess, "run", side_effect=failure), redirect_stderr(output):
            with self.assertRaises(SystemExit) as stopped:
                wheelhouse_acquire.run_shared(failure.cmd, "locate locked wheels")
        self.assertEqual(7, stopped.exception.code)
        self.assertIn("locate locked wheels", output.getvalue())
        self.assertNotIn("secret.invalid", output.getvalue())
        self.assertNotIn("private", output.getvalue())

    def test_support_cli_reports_network_failure_without_traceback(self):
        source_url = "https://github.com/numpde/nmr-provider-tools/releases/download/v1/provider_core-1.0.tar.gz#sha256=" + "d" * 64
        self.assets.write_text(source_url + "\n")
        script = '''
import runpy, sys, urllib.error
from unittest.mock import patch
import tools.wheelhouse as wheelhouse
sys.argv = ["wheelhouse_acquire", "--support", "--assets", sys.argv[1], "--pool", sys.argv[2]]
failure = urllib.error.HTTPError("https://secret.invalid/private", 503, "private body", {}, None)
with patch.object(wheelhouse, "open_url", side_effect=failure):
    runpy.run_module("tools.wheelhouse_acquire", run_name="__main__")
'''
        result = subprocess.run([sys.executable, "-B", "-c", script, str(self.assets), str(self.pool)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("HTTP 503", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotIn("secret.invalid", result.stderr)
        self.assertNotIn("private", result.stderr)

    def test_readiness_distinguishes_present_missing_and_conflicting_wheels(self):
        cached = wheel(self.pool, "pool-alpha", "1.0")
        lock = locked(self.root / "alpha.lock", [("pool-alpha", "1.0", [cached])])
        self.assertTrue(wheelhouse_acquire.readiness([lock], self.pool))
        cached.unlink()
        self.assertFalse(wheelhouse_acquire.readiness([lock], self.pool))
        cached.write_bytes(b"conflicting cached bytes")
        with self.assertRaisesRegex(ValueError, "cached wheel.*differs"):
            wheelhouse_acquire.readiness([lock], self.pool)

    def test_readiness_does_not_treat_pip_failure_with_complete_pool_as_missing(self):
        cached = wheel(self.pool, "pool-alpha", "1.0")
        lock = locked(self.root / "alpha.lock", [("pool-alpha", "1.0", [cached])])
        with patch.object(wheelhouse_acquire, "select_wheels", side_effect=subprocess.CalledProcessError(1, "pip")):
            with self.assertRaises(subprocess.CalledProcessError):
                wheelhouse_acquire.readiness([lock], self.pool)

    def test_readiness_propagates_interruption(self):
        with patch.object(wheelhouse_acquire, "select_wheels", side_effect=subprocess.CalledProcessError(2, "pip")):
            with self.assertRaises(subprocess.CalledProcessError):
                wheelhouse_acquire.readiness([self.lock], self.pool)

    def test_readiness_rejects_invalid_missing_release_declaration(self):
        self.tool_assets.write_text("https://example.invalid/missing.pyz#sha256=" + "a" * 64 + "\n")
        with self.assertRaisesRegex(ValueError, "pinned shared-owner"):
            wheelhouse_acquire.readiness([], self.pool, stream_assets=self.tool_assets)

    def test_conflict_scan_does_not_treat_directory_error_as_missing(self):
        with patch.object(wheelhouse_acquire.os, "scandir", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                wheelhouse_acquire.refuse_conflicting_wheels(self.lock, self.pool)

    def test_tool_only_probe_reports_missing_then_verified(self):
        path = self.pool / "wheelhouse-tool-1.0.0.pyz"
        content = b"tool bytes"
        self.tool_assets.write_text("https://github.com/numpde/nmr-provider-tools/releases/download/v1/"
                                    + path.name + "#sha256=" + hashlib.sha256(content).hexdigest() + "\n")
        argv = ["wheelhouse_acquire", "--probe", "--tool-only", "--assets", str(self.tool_assets), "--pool", str(self.pool)]
        with patch.object(sys, "argv", argv):
            self.assertEqual(3, wheelhouse_acquire.main())
            path.write_bytes(content)
            self.assertEqual(0, wheelhouse_acquire.main())
            path.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "cached bytes differ"):
                wheelhouse_acquire.main()

    def test_support_probe_reports_missing_then_verified(self):
        path = self.pool / "provider_core-1.0.tar.gz"
        content = b"source bytes"
        self.assets.write_text("https://github.com/numpde/nmr-provider-tools/releases/download/v1/"
                               + path.name + "#sha256=" + hashlib.sha256(content).hexdigest() + "\n")
        self.assertFalse(wheelhouse_acquire.test_support(self.assets, self.pool, download=False, probe=True))
        path.write_bytes(content)
        self.assertTrue(wheelhouse_acquire.test_support(self.assets, self.pool, download=False, probe=True))
        path.write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "cached bytes differ"):
            wheelhouse_acquire.test_support(self.assets, self.pool, download=False, probe=True)

    def test_probe_rejects_incompatible_cli_modes(self):
        argv = ["wheelhouse_acquire", "--probe", "--check", "--pool", str(self.pool), "--lock", str(self.lock)]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit) as error:
            wheelhouse_acquire.main()
        self.assertEqual(2, error.exception.code)

    def test_warm_stream_asset_remains_readable_during_another_shared_lease(self):
        content = b"cached release tool"
        name = "wheelhouse-tool-1.0.0.pyz"
        (self.pool / name).write_bytes(content)
        self.tool_assets.write_text("https://github.com/numpde/nmr-provider-tools/releases/download/v1/" + name
                                    + "#sha256=" + hashlib.sha256(content).hexdigest() + "\n")
        failures = []
        def acquire():
            try:
                wheelhouse_acquire.acquire([self.lock], self.pool, self.assets, self.tool_assets,
                                           stream_assets=self.tool_assets)
            except Exception as error:
                failures.append(error)
        with patch.object(wheelhouse_acquire, "select_wheels", return_value={}):
            with wheelhouse_acquire.pool_lock(self.pool, exclusive=False):
                worker = threading.Thread(target=acquire)
                worker.start()
                worker.join(timeout=2)
                completed_while_shared = not worker.is_alive()
            worker.join(timeout=5)
        self.assertTrue(completed_while_shared, "a warm acquisition waited for an exclusive pool lease")
        self.assertFalse(worker.is_alive())
        self.assertEqual([], failures)

    def test_stream_asset_is_rechecked_after_another_publisher_wins(self):
        with (patch.object(wheelhouse_acquire, "shared_asset_present", side_effect=[False, True]),
              patch.object(wheelhouse_acquire, "prepare_core_assets") as prepare,
              patch.object(wheelhouse_acquire, "select_wheels", return_value={})):
            wheelhouse_acquire.acquire([self.lock], self.pool, self.assets, self.tool_assets,
                                       stream_assets=self.tool_assets)
        prepare.assert_not_called()

    def test_interrupted_offline_selection_does_not_start_acquisition(self):
        for status in (-9, 2, 130, 137, 143):
            with self.subTest(status=status):
                failure = subprocess.CalledProcessError(status, "pip")
                with (patch.object(wheelhouse_acquire, "prepare_core_assets") as assets,
                      patch.object(wheelhouse_acquire, "select_wheels", side_effect=failure),
                      patch.object(wheelhouse_acquire.subprocess, "run") as run,
                      self.assertRaises(SystemExit) as interrupted):
                    wheelhouse_acquire.acquire([self.lock], self.pool, self.assets, self.tool_assets)
                self.assertEqual(interrupted.exception.code, 128 - status if status < 0 else status)
                assets.assert_not_called()
                run.assert_not_called()

    def test_warm_pool_still_ensures_the_stream_tool_needed_for_builds(self):
        captured = []

        def prepare(path, pool, *, download):
            captured.append(path.read_text())
            self.assertEqual(pool, self.pool)
            self.assertTrue(download)

        with (patch.object(wheelhouse_acquire, "prepare_core_assets", side_effect=prepare),
              patch.object(wheelhouse_acquire, "select_wheels", return_value={}),
              patch.object(wheelhouse_acquire.subprocess, "run") as run):
            wheelhouse_acquire.acquire([self.lock], self.pool, self.assets, self.tool_assets,
                                       stream_assets=self.tool_assets)
        self.assertEqual(captured, [self.tool_assets.read_text()])
        run.assert_not_called()

    def test_missing_wheel_uses_issuer_source_and_shared_publisher(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            if "locate" in command:
                kwargs["stdout"].write(b"projected lock")
            else:
                # The shared publisher must be able to take its own exclusive lease.
                with wheelhouse_acquire.pool_lock(self.pool, exclusive=True) as descriptor:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return subprocess.CompletedProcess(command, 0)

        with (patch.object(wheelhouse_acquire, "prepare_core_assets"),
              patch.object(wheelhouse_acquire, "select_wheels", side_effect=[subprocess.CalledProcessError(1, "pip"),
                                                                            subprocess.CalledProcessError(1, "pip"), {}]),
              patch.object(wheelhouse_acquire.subprocess, "run", side_effect=run)):
            wheelhouse_acquire.acquire([self.lock], self.pool, self.assets, self.tool_assets,
                                       declarations=[self.declaration])
        self.assertEqual(len(calls), 2)
        self.assertIn("locate", calls[0])
        self.assertEqual(calls[0][calls[0].index("--pool") + 1], str(self.pool))
        self.assertIn(self.url, calls[0])
        self.assertIn("acquire", calls[1])
        self.assertIn("--source-lock", calls[1])
        self.assertEqual(calls[1][-2:], ["--pool", str(self.pool)])

    def test_peer_winner_is_rechecked_before_tool_bootstrap(self):
        with (patch.object(wheelhouse_acquire, "prepare_core_assets") as assets,
              patch.object(wheelhouse_acquire, "select_wheels", side_effect=[subprocess.CalledProcessError(1, "pip"), {}]),
              patch.object(wheelhouse_acquire.subprocess, "run") as run):
            wheelhouse_acquire.acquire([self.lock], self.pool, self.assets, self.tool_assets,
                                       declarations=[self.declaration])
        assets.assert_not_called()
        run.assert_not_called()

    def test_real_peer_publishes_before_locked_recheck_without_network(self):
        source = self.root / "source"
        source.mkdir()
        candidate = wheel(source, "pool-alpha", "1.0")
        lock = locked(self.root / "alpha.lock", [("pool-alpha", "1.0", [candidate])])
        context = multiprocessing.get_context("fork")
        start, published = context.Event(), context.Event()

        def publish():
            if not start.wait(10):
                raise RuntimeError("peer was not released")
            with wheelhouse_acquire.pool_lock(self.pool, exclusive=True):
                os.link(candidate, self.pool / candidate.name)
            published.set()
        peer = context.Process(target=publish)
        peer.start()
        real_lock = wheelhouse_acquire.pool_lock
        first = True

        @contextmanager
        def coordinated(pool, exclusive):
            nonlocal first
            with real_lock(pool, exclusive) as descriptor:
                yield descriptor
            if not exclusive and first:
                first = False
                start.set()
                if not published.wait(10):
                    raise RuntimeError("peer did not publish")
        try:
            with (patch.object(wheelhouse_acquire, "pool_lock", coordinated),
                  patch.object(wheelhouse_acquire, "prepare_core_assets") as prepare):
                wheelhouse_acquire.acquire([lock], self.pool, self.assets, self.tool_assets)
            prepare.assert_not_called()
            peer.join(timeout=10)
            self.assertEqual(0, peer.exitcode)
            self.assertEqual(candidate.stat().st_ino, (self.pool / candidate.name).stat().st_ino)
        finally:
            if peer.is_alive():
                peer.terminate()
            peer.join(timeout=5)

    def test_conflicting_cached_wheel_stops_before_network(self):
        cached = wheel(self.pool, "pool-alpha", "1.0")
        lock = locked(self.root / "alpha.lock", [("pool-alpha", "1.0", [cached])])
        cached.write_bytes(b"conflicting cached bytes")
        with patch.object(wheelhouse_acquire, "prepare_core_assets") as prepare:
            with self.assertRaisesRegex(ValueError, "cached wheel.*differs"):
                wheelhouse_acquire.acquire([lock], self.pool, self.assets, self.tool_assets,
                                           stream_assets=self.tool_assets)
        prepare.assert_not_called()
        self.assertEqual(b"conflicting cached bytes", cached.read_bytes())

    def test_new_conflict_is_rechecked_before_missing_stream_download(self):
        cached = wheel(self.pool, "pool-alpha", "1.0")
        lock = locked(self.root / "alpha.lock", [("pool-alpha", "1.0", [cached])])
        cached.unlink()
        calls = 0
        def select(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                cached.write_bytes(b"conflicting cached bytes")
            raise subprocess.CalledProcessError(1, "pip")
        with (patch.object(wheelhouse_acquire, "select_wheels", side_effect=select),
              patch.object(wheelhouse_acquire, "prepare_core_assets") as prepare):
            with self.assertRaisesRegex(ValueError, "cached wheel.*differs"):
                wheelhouse_acquire.acquire([lock], self.pool, self.assets, self.tool_assets,
                                           stream_assets=self.tool_assets)
        prepare.assert_not_called()
        self.assertEqual(2, calls)

    def test_unused_conflicting_wheel_does_not_reject_valid_alternative(self):
        good = wheel(self.pool, "pool-alpha", "1.0")
        alternate = self.pool / "pool_alpha-1.0-py2.py3-none-any.whl"
        alternate.symlink_to(good)
        lock = locked(self.root / "alpha.lock", [("pool-alpha", "1.0", [good])])
        wheelhouse_acquire.refuse_conflicting_wheels(lock, self.pool)

    def test_conflicting_local_version_matching_public_pin_stops_before_network(self):
        cached = wheel(self.pool, "pool-alpha", "1.0+local")
        lock = locked(self.root / "alpha.lock", [("pool-alpha", "1.0", [cached])])
        cached.write_bytes(b"conflicting cached bytes")
        with patch.object(wheelhouse_acquire, "prepare_core_assets") as prepare:
            with self.assertRaisesRegex(ValueError, "cached wheel.*differs"):
                wheelhouse_acquire.acquire([lock], self.pool, self.assets, self.tool_assets)
        prepare.assert_not_called()
        self.assertEqual(b"conflicting cached bytes", cached.read_bytes())

    def test_invalid_locked_version_is_rejected_without_cached_candidate(self):
        lock = self.root / "invalid.lock"
        lock.write_text("pool-alpha==not-a-version \\\n    --hash=sha256:" + "a" * 64 + "\n")
        with patch.object(wheelhouse_acquire, "prepare_core_assets") as prepare:
            with self.assertRaises(ValueError):
                wheelhouse_acquire.acquire([lock], self.pool, self.assets, self.tool_assets)
        prepare.assert_not_called()

    def test_interrupted_recheck_does_not_bootstrap_tool(self):
        with (patch.object(wheelhouse_acquire, "select_wheels", side_effect=[
                subprocess.CalledProcessError(1, "pip"), subprocess.CalledProcessError(2, "pip")]),
              patch.object(wheelhouse_acquire, "prepare_core_assets") as prepare):
            with self.assertRaises(SystemExit) as interrupted:
                wheelhouse_acquire.acquire([self.lock], self.pool, self.assets, self.tool_assets)
        self.assertEqual(2, interrupted.exception.code)
        prepare.assert_not_called()

    def test_invalid_direct_source_does_not_fall_back_to_index(self):
        self.declaration.write_text("torch @ https://example.invalid/torch.whl\n")
        with self.assertRaisesRegex(ValueError, "direct sources require"):
            wheelhouse_acquire.direct_wheels([self.declaration], {"torch"})
        self.declaration.write_text("torch @ https://example.invalid/torch.whl#sha256=" + "a" * 64 + "\n")
        with self.assertRaisesRegex(ValueError, "invalid wheel filename"):
            wheelhouse_acquire.direct_wheels([self.declaration], {"torch"})

    def test_support_selects_only_the_pinned_source_archive(self):
        source_url = "https://github.com/numpde/nmr-provider-tools/releases/download/v1/provider_core-1.0.tar.gz#sha256=" + "d" * 64
        self.assets.write_text(self.assets.read_text() + source_url + "\n")
        captured = []

        def prepare(path, pool, *, download):
            captured.append(path.read_text())
            self.assertEqual(pool, self.pool)
            self.assertTrue(download)

        with patch.object(wheelhouse_acquire, "prepare_core_assets", side_effect=prepare):
            wheelhouse_acquire.test_support(self.assets, self.pool, download=True)
        self.assertEqual(captured, [source_url + "\n"])

    def test_support_check_does_not_download_when_peer_publishes_after_first_check(self):
        source_url = "https://github.com/numpde/nmr-provider-tools/releases/download/v1/provider_core-1.0.tar.gz#sha256=" + "d" * 64
        self.assets.write_text(source_url + "\n")
        with (patch.object(wheelhouse_acquire, "shared_asset_present", return_value=False),
              patch.object(wheelhouse_acquire, "prepare_core_assets") as prepare):
            wheelhouse_acquire.test_support(self.assets, self.pool, download=False)
        self.assertEqual(1, prepare.call_count)
        self.assertIs(prepare.call_args.kwargs["download"], False)


if __name__ == "__main__":
    unittest.main()
