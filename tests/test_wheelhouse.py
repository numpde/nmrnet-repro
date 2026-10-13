"""Offline publication checks for hash-locked wheels and pinned Uni-Core source."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools import wheelhouse


class WheelhouseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lock = self.root / "inference.lock"
        self.source = self.root / "unicore-source.json"
        self.output = self.root / "cpu"
        self.wheel_name = "fixture-1.0-py3-none-any.whl"
        self.wheel_bytes = b"locked wheel bytes"
        self.source_bytes = b"pinned source bytes"
        self.lock.write_text("fixture==1.0 \\\n    --hash=sha256:" + hashlib.sha256(self.wheel_bytes).hexdigest() + "\n")
        self.source.write_text(json.dumps({
            "name": "unicore", "version": "0.0.1", "commit": "a" * 40,
            "url": "https://codeload.github.com/dptech-corp/Uni-Core/tar.gz/" + "a" * 40,
            "size": len(self.source_bytes), "sha256": hashlib.sha256(self.source_bytes).hexdigest(),
        }))

    def pip_download(self, command, **kwargs):
        self.assertIn("--require-hashes", command)
        self.assertIn("--only-binary=:all:", command)
        self.assertIn("--no-deps", command)
        self.assertNotIn("install", command)
        destination = Path(command[command.index("--dest") + 1])
        (destination / self.wheel_name).write_bytes(self.wheel_bytes)

    def acquire(self, *, pip=None, body=None):
        with patch.object(wheelhouse.subprocess, "run", side_effect=pip or self.pip_download), \
             patch.object(wheelhouse, "open_url", return_value=io.BytesIO(
                 self.source_bytes if body is None else body)):
            wheelhouse.download(self.lock, self.source, self.output)

    def test_complete_publication_verifies_and_is_reused_without_network(self):
        self.acquire()
        wheelhouse.check(self.lock, self.source, self.output)
        with patch.object(wheelhouse.subprocess, "run", side_effect=AssertionError("pip called")), \
             patch.object(wheelhouse, "open_url", side_effect=AssertionError("network")):
            wheelhouse.download(self.lock, self.source, self.output)

    def test_publication_syncs_contents_then_stage_then_published_parent(self):
        events = []
        fsync = os.fsync
        publish = wheelhouse.publish_directory

        def synced(descriptor):
            path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            events.append(("sync", path))
            fsync(descriptor)

        def published(stage, output):
            events.append(("publish", stage))
            publish(stage, output)

        with patch.object(wheelhouse.os, "fsync", side_effect=synced), \
             patch.object(wheelhouse, "publish_directory", side_effect=published):
            self.acquire()

        stage = next(path for action, path in events if action == "publish")
        self.assertCountEqual(events[:3], [
            ("sync", stage / self.wheel_name),
            ("sync", stage / wheelhouse.SOURCE_NAME),
            ("sync", stage / ".complete"),
        ])
        self.assertEqual(events[3:], [
            ("sync", stage), ("publish", stage), ("sync", self.output.parent),
        ])

    def test_prepublication_sync_failure_preserves_stage_without_final(self):
        for failure in (self.wheel_name, wheelhouse.SOURCE_NAME, ".complete", "stage"):
            with self.subTest(failure=failure):
                before = set(self.root.glob(".cpu.pending-*"))

                def fail_sync(descriptor):
                    path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
                    if path.name == failure or (failure == "stage" and path.is_dir()):
                        raise OSError("simulated sync failure")

                with patch.object(wheelhouse.os, "fsync", side_effect=fail_sync):
                    with self.assertRaisesRegex(OSError, "simulated sync failure"):
                        self.acquire()
                self.assertFalse(self.output.exists())
                retained = set(self.root.glob(".cpu.pending-*")) - before
                self.assertEqual(len(retained), 1)
                stage = retained.pop()
                self.assertEqual((stage / self.wheel_name).read_bytes(), self.wheel_bytes)
                self.assertEqual((stage / wheelhouse.SOURCE_NAME).read_bytes(), self.source_bytes)
                self.assertTrue((stage / ".complete").is_file())

    def test_wrong_wheel_hash_never_publishes(self):
        def corrupt(command, **kwargs):
            self.pip_download(command, **kwargs)
            (Path(command[command.index("--dest") + 1]) / self.wheel_name).write_bytes(b"corrupt")
        with self.assertRaisesRegex(ValueError, "hash"):
            self.acquire(pip=corrupt)
        self.assertFalse(self.output.exists())

    def test_unlisted_or_missing_wheel_never_publishes(self):
        for name in ("extra-1.0-py3-none-any.whl", "fixture-2.0-py3-none-any.whl"):
            with self.subTest(name=name):
                def wrong(command, **kwargs):
                    destination = Path(command[command.index("--dest") + 1])
                    (destination / name).write_bytes(self.wheel_bytes)
                with self.assertRaises(ValueError):
                    self.acquire(pip=wrong)
                self.assertFalse(self.output.exists())

    def test_wrong_or_excess_source_bytes_never_publish(self):
        for body in (b"X" * len(self.source_bytes), self.source_bytes + b"extra"):
            with self.subTest(size=len(body)):
                with self.assertRaisesRegex(ValueError, "source"):
                    self.acquire(body=body)
                self.assertFalse(self.output.exists())

    def test_interrupted_download_preserves_incomplete_staging(self):
        def interrupted(command, **kwargs):
            self.pip_download(command, **kwargs)
            raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError):
            self.acquire(pip=interrupted)
        self.assertFalse(self.output.exists())
        self.assertEqual(len(list(self.root.glob(".cpu.pending-*"))), 1)

    def test_existing_corrupt_wheelhouse_is_preserved(self):
        self.acquire()
        wheel = self.output / self.wheel_name
        wheel.write_bytes(b"modified")
        with self.assertRaises(ValueError):
            self.acquire()
        self.assertEqual(wheel.read_bytes(), b"modified")

    def test_lock_drift_rejects_existing_wheelhouse(self):
        self.acquire()
        self.lock.write_text(self.lock.read_text() + "# changed\n")
        with self.assertRaisesRegex(ValueError, "lock"):
            wheelhouse.check(self.lock, self.source, self.output)

    def test_symlinked_wheel_is_rejected(self):
        self.acquire()
        wheel = self.output / self.wheel_name
        wheel.unlink()
        target = self.root / "outside"
        target.write_bytes(self.wheel_bytes)
        wheel.symlink_to(target)
        with self.assertRaises((ValueError, OSError)):
            wheelhouse.check(self.lock, self.source, self.output)

    def test_publication_never_replaces_a_concurrent_output(self):
        publish = wheelhouse.publish_directory
        def concurrent(stage, output):
            output.mkdir()
            (output / "keep").write_bytes(b"existing")
            publish(stage, output)
        with patch.object(wheelhouse, "publish_directory", side_effect=concurrent):
            with self.assertRaises(FileExistsError):
                self.acquire()
        self.assertEqual((self.output / "keep").read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
