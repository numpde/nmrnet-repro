"""Offline checks for pinned source bytes, capture identity and publication durability."""

import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import wheelhouse


class WheelhouseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "shared pool"
        self.pool.mkdir()
        self.lock = self.root / "inference.lock"
        self.source = self.root / "unicore-source.json"
        self.capture = self.root / "private capture"
        self.capture.mkdir()
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

    def capture_selected(self, source_path=None):
        with patch.object(wheelhouse, "select_wheels", return_value=self.selected_wheels()):
            return wheelhouse.capture(self.lock, source_path, self.pool, self.capture)

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
        self.assertEqual([], list(self.pool.glob(".unicore-source-*")))

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
        self.assertEqual([], list(self.pool.glob(".unicore-source-*")))

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
                self.assertEqual([], list(self.pool.glob(".unicore-source-*")))

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

    def test_capture_returns_identity_for_the_copied_selected_bytes(self):
        before = {path.name: path.read_bytes() for path in self.pool.iterdir()}
        identity = self.capture_selected()
        self.assertEqual({self.wheel_name}, {path.name for path in self.capture.iterdir()})
        self.assertEqual(self.wheel_bytes, (self.capture / self.wheel_name).read_bytes())
        self.assertEqual(0o644, (self.capture / self.wheel_name).stat().st_mode & 0o777)
        self.assertEqual(
            {
                "lock_sha256": wheelhouse.digest(self.lock),
                "source_declaration_sha256": None,
                "files": {self.wheel_name: hashlib.sha256(self.wheel_bytes).hexdigest()},
            },
            identity,
        )
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.pool.iterdir()})

    def test_source_bearing_capture_returns_source_declaration_and_archive_hashes(self):
        source_identity = wheelhouse.source_identity(self.source)
        archive = wheelhouse.source_cache_file(self.pool, source_identity)
        archive.write_bytes(self.source_bytes)
        identity = self.capture_selected(self.source)
        self.assertEqual(
            {self.wheel_name, wheelhouse.SOURCE_NAME},
            {path.name for path in self.capture.iterdir()},
        )
        self.assertEqual(self.source_bytes, (self.capture / wheelhouse.SOURCE_NAME).read_bytes())
        self.assertEqual(wheelhouse.digest(self.source), identity["source_declaration_sha256"])
        self.assertEqual(hashlib.sha256(self.source_bytes).hexdigest(), identity["files"][wheelhouse.SOURCE_NAME])

    def test_capture_refuses_copied_bytes_that_differ_from_pip_selection(self):
        selected = self.selected_wheels()

        copyfileobj = wheelhouse.shutil.copyfileobj

        def changed_copy(source, destination, length=0):
            copyfileobj(source, destination, length)
            destination.seek(0)
            destination.write(b"changed copied bytes")

        with (
            patch.object(wheelhouse, "select_wheels", return_value=selected),
            patch.object(wheelhouse.shutil, "copyfileobj", side_effect=changed_copy) as copying,
            self.assertRaises(ValueError),
        ):
            wheelhouse.capture(self.lock, None, self.pool, self.capture)
        copying.assert_called_once()

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

    def test_cli_requires_one_lock_for_private_capture(self):
        build_lock = self.root / "second.lock"
        build_lock.write_text(self.lock.read_text())
        arguments = [
            "wheelhouse",
            "capture",
            "--profile",
            "preparation",
            "--pool",
            str(self.pool),
            "--output",
            str(self.capture),
            "--lock",
            str(self.lock),
            "--lock",
            str(build_lock),
        ]
        with (
            patch.object(wheelhouse.sys, "argv", arguments),
            patch.object(wheelhouse.sys, "stderr", io.StringIO()),
            patch.object(wheelhouse, "capture") as capture,
            self.assertRaises(SystemExit) as caught,
        ):
            wheelhouse.main()
        self.assertEqual(2, caught.exception.code)
        capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
