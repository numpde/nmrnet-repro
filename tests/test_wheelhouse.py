"""Offline checks for pinned source bytes and publication durability."""

import fcntl
import hashlib
import io
import json
import os
import socket
import ssl
import tempfile
import traceback
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tools import wheelhouse


class WheelhouseTests(unittest.TestCase):
    def test_shared_tool_writer_holds_pool_lock(self):
        assets = self.root / "tool-assets.in"
        assets.write_text("pinned release input")
        arguments = ["wheelhouse", "download", "--profile", "shared-tool", "--assets", str(assets),
                     "--pool", str(self.pool)]

        def assert_pool_locked(*_args, **_kwargs):
            descriptor = os.open(self.pool, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)

        with (patch.object(wheelhouse.sys, "argv", arguments),
              patch.object(wheelhouse, "prepare_core_assets", side_effect=assert_pool_locked)):
            wheelhouse.main()

    def test_shared_tool_reader_holds_shared_pool_lock(self):
        assets = self.root / "tool-assets.in"
        assets.write_text("pinned release input")
        arguments = ["wheelhouse", "check", "--profile", "shared-tool", "--assets", str(assets),
                     "--pool", str(self.pool)]

        def assert_pool_read_locked(*_args, **_kwargs):
            descriptor = os.open(self.pool, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)

        with (patch.object(wheelhouse.sys, "argv", arguments),
              patch.object(wheelhouse, "prepare_core_assets", side_effect=assert_pool_read_locked)):
            wheelhouse.main()

    def test_shared_tool_profile_checks_only_its_release_assets(self):
        assets = self.root / "tool-assets.in"
        assets.write_text("pinned release input")
        for action in ("download", "check"):
            arguments = ["wheelhouse", action, "--profile", "shared-tool", "--assets", str(assets),
                         "--pool", str(self.pool)]
            with (patch.object(wheelhouse.sys, "argv", arguments),
                  patch.object(wheelhouse, "prepare_core_assets") as prepare,
                  patch.object(wheelhouse, "select_wheels") as select,
                  patch.object(wheelhouse, "populate_pool") as populate):
                wheelhouse.main()
            prepare.assert_called_once_with(assets, self.pool, download=action == "download")
            select.assert_not_called()
            populate.assert_not_called()

    def test_report_uses_captured_lock_even_if_original_changes(self):
        expected = self.lock.read_bytes()
        output = io.StringIO()

        def select(lock, pool, report_output, lock_fd):
            self.lock.write_bytes(b"later lock")
            self.assertEqual(lock.read_bytes(), expected)
            self.assertEqual(pool, self.pool)
            self.assertGreaterEqual(lock_fd, 0)
            report_output.write('{"version":"1"}')

        with (patch.object(wheelhouse.sys, "argv", ["wheelhouse", "report", "--lock", str(self.lock),
                                                    "--lock-sha256", hashlib.sha256(expected).hexdigest(),
                                                    "--pool", str(self.pool)]),
              patch.object(wheelhouse.sys, "stdout", output),
              patch.object(wheelhouse, "select_wheels", side_effect=select)):
            wheelhouse.main()
        self.assertEqual(json.loads(output.getvalue()), {"version": "1"})

    def test_report_rejects_other_input_authorities(self):
        common = ["wheelhouse", "report", "--lock", str(self.lock), "--lock-sha256", wheelhouse.digest(self.lock),
                  "--pool", str(self.pool)]
        for extra in (["--lock", str(self.lock)], ["--profile", "preparation"],
                      ["--source", str(self.source)], ["--assets", str(self.source)]):
            with self.subTest(extra=extra), patch.object(wheelhouse.sys, "argv", common + extra):
                with patch.object(wheelhouse.sys, "stderr", io.StringIO()), self.assertRaises(SystemExit):
                    wheelhouse.main()

    def test_report_refuses_lock_identity_drift_before_selection(self):
        arguments = ["wheelhouse", "report", "--lock", str(self.lock), "--lock-sha256", "0" * 64,
                     "--pool", str(self.pool)]
        with (patch.object(wheelhouse.sys, "argv", arguments), patch.object(wheelhouse, "select_wheels") as select,
              self.assertRaisesRegex(ValueError, "captured lock bytes differ")):
            wheelhouse.main()
        select.assert_not_called()

    def test_native_core_lock_needs_no_third_party_download(self):
        self.lock.write_text("provider-core==0.5.0 \\\n" + "    --hash=sha256:" + "a" * 64 + "\n")
        assets = self.root / "core-assets.in"
        assets.write_text("fixture release inputs")
        with (
            patch.object(wheelhouse.sys, "argv", ["wheelhouse", "download", "--profile", "provider-core",
                                                  "--lock", str(self.lock), "--assets", str(assets),
                                                  "--pool", str(self.pool)]),
            patch.object(wheelhouse, "prepare_core_assets") as prepare,
            patch.object(wheelhouse, "populate_pool") as populate,
            patch.object(wheelhouse, "select_wheels") as select,
        ):
            wheelhouse.main()
        prepare.assert_called_once_with(assets, self.pool, download=True)
        populate.assert_not_called()
        select.assert_called_once()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "shared pool"
        self.pool.mkdir()
        self.lock = self.root / "inference.lock"
        self.source = self.root / "unicore-source.json"
        self.wheel_name = "fixture-1.0-py3-none-any.whl"
        self.wheel_bytes = b"locked wheel bytes"
        self.source_bytes = b"pinned source bytes"
        self.wheel = self.pool / self.wheel_name
        self.wheel.write_bytes(self.wheel_bytes)
        self.lock.write_text("fixture==1.0 \\\n    --hash=sha256:" + hashlib.sha256(self.wheel_bytes).hexdigest() + "\n")
        self.source.write_text(
            json.dumps(
                {
                    "name": "unicore",
                    "version": "0.0.1",
                    "commit": "a" * 40,
                    "url": "https://codeload.github.com/dptech-corp/Uni-Core/tar.gz/" + "a" * 40,
                    "size": len(self.source_bytes),
                    "sha256": hashlib.sha256(self.source_bytes).hexdigest(),
                }
            )
        )

    def selected_wheels(self):
        return {
            "lock_sha256": wheelhouse.digest(self.lock),
            "source_declaration_sha256": None,
            "files": {self.wheel_name: wheelhouse.digest(self.wheel)},
        }

    def test_source_identity_retains_every_declared_field(self):
        self.assertEqual(json.loads(self.source.read_text()), wheelhouse.source_identity(self.source))

    def test_missing_source_fields_reject_cleanly(self):
        document = json.loads(self.source.read_text())
        for field in document:
            with self.subTest(field=field):
                self.source.write_text(json.dumps({key: value for key, value in document.items() if key != field}))
                with self.assertRaisesRegex(ValueError, "Cannot acquire Uni-Core source: declaration fields"):
                    wheelhouse.source_identity(self.source)

    def test_extra_source_authority_rejects_before_staging_or_network(self):
        document = json.loads(self.source.read_text())
        document["sha256_override"] = "unreviewed"
        self.source.write_text(json.dumps(document))
        with (
            patch.object(wheelhouse, "open_url") as network,
            self.assertRaisesRegex(ValueError, "Cannot acquire Uni-Core source: declaration fields"),
        ):
            wheelhouse.prepare_source(self.source, self.pool)
        network.assert_not_called()
        self.assertEqual([], list(self.pool.glob(".unicore-source.*")))

    def test_non_object_source_declaration_rejects_cleanly(self):
        for document in (None, []):
            with self.subTest(document=document):
                self.source.write_text(json.dumps(document))
                with self.assertRaisesRegex(ValueError, "Cannot acquire Uni-Core source: declaration fields"):
                    wheelhouse.source_identity(self.source)

    def test_pinned_source_is_acquired_once_and_reused_by_declared_identity(self):
        with patch.object(wheelhouse, "open_url", return_value=io.BytesIO(self.source_bytes)) as network:
            wheelhouse.prepare_source(self.source, self.pool)
            wheelhouse.prepare_source(self.source, self.pool)
        self.assertEqual(network.call_count, 1)
        source = wheelhouse.source_cache_file(self.pool, wheelhouse.source_identity(self.source))
        self.assertEqual(self.source_bytes, source.read_bytes())
        self.assertFalse(source.is_symlink())
        self.assertEqual([], list(self.pool.glob(".unicore-source.*")))

    def test_direct_asset_network_failures_are_bounded_and_remove_partial(self):
        assets = self.root / "assets.in"
        name = "wheelhouse-tool-1.0.0.pyz"
        assets.write_text(f"https://github.com/numpde/nmr-provider-tools/releases/download/v1/{name}#sha256=" + "a" * 64 + "\n")
        failures = [
            (urllib.error.HTTPError("https://secret.invalid/path", 503, "private body", {}, None), "HTTP 503"),
            (urllib.error.URLError(socket.gaierror(-2, "private resolver")), "name resolution failed"),
            (urllib.error.URLError(ssl.SSLError("private certificate")), "TLS connection failed"),
            (urllib.error.URLError(OSError("Tunnel connection failed: 502 private proxy")), "proxy tunnel returned HTTP 502"),
        ]
        for error, detail in failures:
            with self.subTest(detail=detail), patch.object(wheelhouse, "open_url", side_effect=error) as network:
                with self.assertRaises(wheelhouse.WheelhouseNetworkError) as captured:
                    wheelhouse.prepare_core_assets(assets, self.pool, download=True)
                network.assert_called_once()
                rendered = "".join(traceback.format_exception(captured.exception))
                self.assertIn(detail, rendered)
                self.assertNotIn("private", rendered)
                self.assertNotIn("secret.invalid", rendered)
                self.assertFalse((self.pool / name).exists())
                self.assertEqual([], list(self.pool.glob(f".{name}.*.part")))

    def test_source_read_timeout_preserves_existing_pool_and_removes_partial(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value = response
        response.read.side_effect = TimeoutError("private endpoint")
        with patch.object(wheelhouse, "open_url", return_value=response) as network:
            with self.assertRaises(wheelhouse.WheelhouseNetworkError) as captured:
                wheelhouse.prepare_source(self.source, self.pool)
        network.assert_called_once()
        self.assertIn("connection timed out", str(captured.exception))
        self.assertNotIn("private", "".join(traceback.format_exception(captured.exception)))
        self.assertEqual(self.wheel_bytes, self.wheel.read_bytes())
        self.assertEqual([], list(self.pool.glob(".unicore-source.*")))

    def test_wrong_or_excess_source_bytes_are_rejected_without_retaining_staging(self):
        for body in (b"X" * len(self.source_bytes), self.source_bytes + b"extra"):
            with self.subTest(size=len(body)):
                with (
                    patch.object(wheelhouse, "open_url", return_value=io.BytesIO(body)),
                    self.assertRaisesRegex(ValueError, "source"),
                ):
                    wheelhouse.prepare_source(self.source, self.pool)
                source = wheelhouse.source_cache_file(self.pool, wheelhouse.source_identity(self.source))
                self.assertFalse(source.exists())
                self.assertEqual([], list(self.pool.glob(".unicore-source.*")))

    def test_conflicting_cached_source_is_preserved_without_network(self):
        source = wheelhouse.source_cache_file(self.pool, wheelhouse.source_identity(self.source))
        source.write_bytes(b"different source")
        with (
            patch.object(wheelhouse, "open_url") as network,
            self.assertRaisesRegex(ValueError, "cached size differs"),
        ):
            wheelhouse.prepare_source(self.source, self.pool)
        network.assert_not_called()
        self.assertEqual(b"different source", source.read_bytes())

    def test_per_file_publication_syncs_bytes_before_link_then_pool_directory(self):
        staged = self.root / self.wheel_name
        staged.write_bytes(self.wheel_bytes)
        destination = self.pool / "published-1.0-py3-none-any.whl"
        events = []
        fsync = os.fsync
        link = os.link

        def synced(descriptor):
            path = Path(f"/proc/self/fd/{descriptor}").readlink()
            events.append(("sync", path))
            fsync(descriptor)

        def linked(source, target, **kwargs):
            events.append(("link", Path(source), Path(target)))
            link(source, target, **kwargs)

        with (
            patch.object(wheelhouse.os, "fsync", side_effect=synced),
            patch.object(wheelhouse.os, "link", side_effect=linked),
        ):
            wheelhouse.publish_file(staged, destination, hashlib.sha256(self.wheel_bytes).hexdigest())

        self.assertEqual(
            [("sync", staged), ("link", staged, destination), ("sync", self.pool)],
            events,
        )
        self.assertEqual(self.wheel_bytes, destination.read_bytes())

    def test_per_file_sync_failure_does_not_publish_or_remove_stage(self):
        staged = self.root / self.wheel_name
        staged.write_bytes(self.wheel_bytes)
        destination = self.pool / "published-1.0-py3-none-any.whl"
        with (
            patch.object(wheelhouse.os, "fsync", side_effect=OSError("sync failed")),
            self.assertRaisesRegex(OSError, "sync failed"),
        ):
            wheelhouse.publish_file(staged, destination, hashlib.sha256(self.wheel_bytes).hexdigest())
        self.assertFalse(destination.exists())
        self.assertEqual(self.wheel_bytes, staged.read_bytes())

    def test_cli_rejects_source_mismatch_and_keeps_core_out_of_index_acquisition(self):
        common = ["wheelhouse", "download", "--lock", str(self.lock), "--pool", str(self.pool)]
        invalid = (
            [*common, "--profile", "inference"],
            [*common, "--profile", "preparation", "--source", str(self.source)],
            [*common, "--profile", "provider-core", "--source", str(self.source)],
        )
        for arguments in invalid:
            with (
                self.subTest(arguments=arguments),
                patch.object(wheelhouse.sys, "argv", arguments),
                patch.object(wheelhouse.sys, "stderr", io.StringIO()),
                patch.object(wheelhouse, "populate_pool") as populate,
                self.assertRaises(SystemExit) as caught,
            ):
                wheelhouse.main()
            self.assertEqual(2, caught.exception.code)
            populate.assert_not_called()

        assets = self.root / "core-assets.in"
        assets.write_text("pinned release inputs")
        self.lock.write_text(
            self.lock.read_text() + "provider-core==0.2.0 \\\n"
            + "    --hash=sha256:" + "a" * 64 + "\n"
        )
        arguments = [*common, "--profile", "provider-core", "--assets", str(assets)]
        expected = self.lock.read_bytes()
        received = []

        def populate(lock, pool, *, inference):
            received.append(lock.read_bytes())
            self.assertEqual(pool, self.pool)
            self.assertFalse(inference)
            self.assertNotIn(b"provider-core", lock.read_bytes())
            self.lock.write_bytes(b"later installation lock")

        with (
            patch.object(wheelhouse.sys, "argv", arguments),
            patch.object(wheelhouse, "prepare_core_assets") as prepare,
            patch.object(wheelhouse, "populate_pool", side_effect=populate),
            patch.object(wheelhouse, "select_wheels") as select,
        ):
            wheelhouse.main()
        prepare.assert_called_once_with(assets, self.pool, download=True)
        self.assertEqual(len(received), 1)
        self.assertEqual(received, [expected.split(b"provider-core==", 1)[0]])
        self.assertNotEqual(expected, self.lock.read_bytes())
        select.assert_called_once()

    def test_scientific_acquisition_keeps_sdk_and_scientific_locks_separate(self):
        sdk = self.root / "sdk.lock"
        sdk.write_text(self.lock.read_text() + "provider-core==0.2.3 \\\n    --hash=sha256:" + "a" * 64 + "\n")
        assets = self.root / "assets.in"
        assets.write_text("owned GitHub release pins")
        inputs = []

        def acquired(lock, pool, *, inference):
            inputs.append(lock.read_bytes())
            self.assertEqual(pool, self.pool)
            self.assertFalse(inference)
            self.assertNotIn(b"provider-core", inputs[-1])

        with (
            patch.object(wheelhouse.sys, "argv", [
                "wheelhouse", "download", "--profile", "preparation",
                "--lock", str(self.lock), "--lock", str(sdk),
                "--assets", str(assets), "--pool", str(self.pool),
            ]),
            patch.object(wheelhouse, "prepare_core_assets") as prepare,
            patch.object(wheelhouse, "populate_pool", side_effect=acquired),
            patch.object(wheelhouse, "select_wheels") as verify,
        ):
            wheelhouse.main()
        self.assertEqual(inputs, [self.lock.read_bytes(), self.lock.read_bytes()])
        prepare.assert_called_once_with(assets, self.pool, download=True)
        verify.assert_called_once()



if __name__ == "__main__":
    unittest.main()
