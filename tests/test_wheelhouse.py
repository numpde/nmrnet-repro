"""Offline publication checks for hash-locked wheels and pinned Uni-Core source."""

import functools
import hashlib
import io
import json
import os
import shutil
import subprocess
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
        self.lock = self.root / "inference.lock"
        self.source = self.root / "unicore-source.json"
        self.output = self.root / "cpu"
        self.wheel_name = "fixture-1.0-py3-none-any.whl"
        self.wheel_bytes = b"locked wheel bytes"
        self.source_bytes = b"pinned source bytes"
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

    def pip_download(self, command, **kwargs):
        self.assertIn("--require-hashes", command)
        self.assertIn("--only-binary=:all:", command)
        self.assertIn("--no-deps", command)
        self.assertNotIn("install", command)
        destination = Path(command[command.index("--dest") + 1])
        (destination / self.wheel_name).write_bytes(self.wheel_bytes)

    def acquire(self, *, pip=None, body=None):
        with (
            patch.object(wheelhouse.subprocess, "run", side_effect=pip or self.pip_download),
            patch.object(wheelhouse, "open_url", return_value=io.BytesIO(self.source_bytes if body is None else body)),
        ):
            wheelhouse.download(self.lock, self.source, self.output)

    def test_source_identity_retains_every_declared_field(self):
        self.assertEqual(json.loads(self.source.read_text()), wheelhouse.source_identity(self.source))

    def test_preparation_wheels_publish_verify_and_reuse_without_source_or_torch_index(self):
        with (
            patch.object(wheelhouse.subprocess, "run", side_effect=self.pip_download) as pip,
            patch.object(wheelhouse, "open_url") as source_network,
            patch.object(wheelhouse, "source_identity") as source_identity,
        ):
            wheelhouse.download(self.lock, None, self.output)
            self.assertNotIn("--find-links", pip.call_args.args[0])
            source_network.assert_not_called()
            source_identity.assert_not_called()
            receipt = json.loads((self.output / ".complete").read_bytes())
            self.assertIsNone(receipt["source_declaration_sha256"])
            self.assertEqual({self.wheel_name: hashlib.sha256(self.wheel_bytes).hexdigest()}, receipt["files"])
            wheelhouse.check(self.lock, None, self.output)
            wheelhouse.download(self.lock, None, self.output)
            self.assertEqual(1, pip.call_count)
        (self.output / wheelhouse.SOURCE_NAME).write_bytes(self.source_bytes)
        with self.assertRaisesRegex(ValueError, "unexpected file"):
            wheelhouse.check(self.lock, None, self.output)
        (self.output / wheelhouse.SOURCE_NAME).unlink()
        (self.output / self.wheel_name).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "hash"):
            wheelhouse.check(self.lock, None, self.output)

    def test_cli_rejects_profile_source_mismatch_before_files_or_network(self):
        common = ["wheelhouse", "download", "--lock", str(self.lock), "--output", str(self.output)]
        for arguments in (
            common,
            [*common, "--profile", "inference"],
            [*common, "--profile", "preparation", "--source", str(self.source)],
            [*common, "--profile", "provider-core", "--source", str(self.source)],
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(wheelhouse.sys, "argv", arguments),
                patch.object(wheelhouse.sys, "stderr", io.StringIO()),
                patch.object(wheelhouse, "download") as download,
                self.assertRaises(SystemExit) as caught,
            ):
                wheelhouse.main()
            self.assertEqual(2, caught.exception.code)
            download.assert_not_called()
            self.assertFalse(self.output.exists())
        with (
            patch.object(wheelhouse.sys, "argv", [*common, "--profile", "preparation"]),
            patch.object(wheelhouse, "download") as download,
        ):
            wheelhouse.main()
        download.assert_called_once_with(self.lock, None, self.output)
        with (
            patch.object(wheelhouse.sys, "argv", [*common, "--profile", "provider-core"]),
            patch.object(wheelhouse, "download") as download,
        ):
            wheelhouse.main()
        download.assert_called_once_with(self.lock, None, self.output)

    def test_unknown_source_authority_rejects_before_staging_or_acquisition(self):
        document = json.loads(self.source.read_text())
        document["sha256_override"] = "unreviewed"
        self.source.write_text(json.dumps(document))
        with (
            patch.object(wheelhouse.subprocess, "run") as pip,
            patch.object(wheelhouse, "open_url") as network,
            self.assertRaisesRegex(ValueError, "Cannot acquire Uni-Core source: declaration fields"),
        ):
            wheelhouse.download(self.lock, self.source, self.output)
        pip.assert_not_called()
        network.assert_not_called()
        self.assertFalse(self.output.exists())
        self.assertEqual([], list(self.root.glob(".cpu.pending-*")))

    def test_missing_source_fields_reject_cleanly(self):
        document = json.loads(self.source.read_text())
        for field in document:
            with self.subTest(field=field):
                self.source.write_text(json.dumps({key: value for key, value in document.items() if key != field}))
                with self.assertRaisesRegex(ValueError, "Cannot acquire Uni-Core source: declaration fields"):
                    wheelhouse.source_identity(self.source)

    def test_non_object_source_declaration_rejects_cleanly(self):
        for document in (None, []):
            with self.subTest(document=document):
                self.source.write_text(json.dumps(document))
                with self.assertRaisesRegex(ValueError, "Cannot acquire Uni-Core source: declaration fields"):
                    wheelhouse.source_identity(self.source)

    def test_complete_publication_verifies_and_is_reused_without_network(self):
        self.acquire()
        wheelhouse.check(self.lock, self.source, self.output)
        with (
            patch.object(wheelhouse.subprocess, "run", side_effect=AssertionError("pip called")),
            patch.object(wheelhouse, "open_url", side_effect=AssertionError("network")),
        ):
            wheelhouse.download(self.lock, self.source, self.output)

    def test_publication_syncs_contents_then_stage_then_published_parent(self):
        events = []
        fsync = os.fsync
        publish = wheelhouse.publish_directory

        def synced(descriptor):
            path = Path(f"/proc/self/fd/{descriptor}").readlink()
            events.append(("sync", path))
            fsync(descriptor)

        def published(stage, output):
            events.append(("publish", stage))
            publish(stage, output)

        with (
            patch.object(wheelhouse.os, "fsync", side_effect=synced),
            patch.object(wheelhouse, "publish_directory", side_effect=published),
        ):
            self.acquire()

        stage = next(path for action, path in events if action == "publish")
        self.assertCountEqual(
            [
                ("sync", stage / self.wheel_name),
                ("sync", stage / wheelhouse.SOURCE_NAME),
                ("sync", stage / ".complete"),
            ],
            events[:3],
        )
        self.assertEqual(
            [
                ("sync", stage),
                ("publish", stage),
                ("sync", self.output.parent),
            ],
            events[3:],
        )

    def test_prepublication_sync_failure_preserves_stage_without_final(self):
        for failure in (self.wheel_name, wheelhouse.SOURCE_NAME, ".complete", "stage"):
            with self.subTest(failure=failure):
                before = set(self.root.glob(".cpu.pending-*"))

                def fail_sync(failure, descriptor):
                    path = Path(f"/proc/self/fd/{descriptor}").readlink()
                    if path.name == failure or (failure == "stage" and path.is_dir()):
                        raise OSError("simulated sync failure")

                # Bind the selected fault while fsync supplies its descriptor.
                with (
                    patch.object(wheelhouse.os, "fsync", side_effect=functools.partial(fail_sync, failure)),
                    self.assertRaisesRegex(OSError, "simulated sync failure"),
                ):
                    self.acquire()
                self.assertFalse(self.output.exists())
                retained = set(self.root.glob(".cpu.pending-*")) - before
                self.assertEqual(1, len(retained))
                stage = retained.pop()
                self.assertEqual(self.wheel_bytes, (stage / self.wheel_name).read_bytes())
                self.assertEqual(self.source_bytes, (stage / wheelhouse.SOURCE_NAME).read_bytes())
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

                def wrong(name, command, **kwargs):
                    destination = Path(command[command.index("--dest") + 1])
                    (destination / name).write_bytes(self.wheel_bytes)

                with self.assertRaises(ValueError):
                    # Keep the bad wheel name bound to this invocation's fault.
                    self.acquire(pip=functools.partial(wrong, name))
                self.assertFalse(self.output.exists())

    def test_missing_locked_wheel_names_package_and_version(self):
        self.acquire()
        (self.output / self.wheel_name).unlink()
        with self.assertRaisesRegex(ValueError, r"missing locked wheel fixture==1\.0"):
            wheelhouse.check(self.lock, self.source, self.output)

    def test_missing_unicore_source_names_the_archive(self):
        self.acquire()
        (self.output / wheelhouse.SOURCE_NAME).unlink()
        with self.assertRaisesRegex(ValueError, r"missing Uni-Core source unicore-source\.tar\.gz"):
            wheelhouse.check(self.lock, self.source, self.output)

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
        self.assertEqual(1, len(list(self.root.glob(".cpu.pending-*"))))

    def test_existing_corrupt_wheelhouse_is_preserved(self):
        self.acquire()
        wheel = self.output / self.wheel_name
        wheel.write_bytes(b"modified")
        with self.assertRaises(ValueError):
            self.acquire()
        self.assertEqual(b"modified", wheel.read_bytes())

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

        with patch.object(wheelhouse, "publish_directory", side_effect=concurrent), self.assertRaises(FileExistsError):
            self.acquire()
        self.assertEqual(b"existing", (self.output / "keep").read_bytes())
        self.assertEqual(1, len(list(self.root.glob(".cpu.pending-*"))))

    def test_identical_verified_concurrent_winner_is_reused_and_only_own_stage_removed(self):
        publish = wheelhouse.publish_directory
        peer_stage = self.root / ".cpu.pending-peer"
        peer_stage.mkdir()
        (peer_stage / "keep").write_bytes(b"peer's unfinished acquisition")

        def concurrent(stage, output):
            shutil.copytree(stage, output)
            self.winner_inode = output.stat().st_ino
            publish(stage, output)

        with patch.object(wheelhouse, "publish_directory", side_effect=concurrent):
            self.acquire()
        self.assertEqual(self.output.stat().st_ino, self.winner_inode)
        self.assertEqual([peer_stage], list(self.root.glob(".cpu.pending-*")))
        self.assertEqual((peer_stage / "keep").read_bytes(), b"peer's unfinished acquisition")
        wheelhouse.check(self.lock, self.source, self.output)

    def test_different_lock_permitted_concurrent_bytes_preserve_both_sets(self):
        publish = wheelhouse.publish_directory
        alternate = b"another permitted platform wheel"
        self.lock.write_text(
            self.lock.read_text().rstrip() + " \\\n    --hash=sha256:" + hashlib.sha256(alternate).hexdigest() + "\n"
        )

        def concurrent(stage, output):
            shutil.copytree(stage, output)
            (output / self.wheel_name).write_bytes(alternate)
            (output / ".complete").write_text(json.dumps(wheelhouse.inventory(self.lock, self.source, output)))
            publish(stage, output)

        with patch.object(wheelhouse, "publish_directory", side_effect=concurrent), self.assertRaises(FileExistsError):
            self.acquire()
        self.assertEqual((self.output / self.wheel_name).read_bytes(), alternate)
        (stage,) = self.root.glob(".cpu.pending-*")
        self.assertEqual((stage / self.wheel_name).read_bytes(), self.wheel_bytes)
        wheelhouse.check(self.lock, self.source, self.output)


if __name__ == "__main__":
    unittest.main()
