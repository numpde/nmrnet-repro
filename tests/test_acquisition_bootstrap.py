"""The cold acquisition closure must match its pinned release inventory."""

import hashlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acquisition_bootstrap", ROOT / "tools/acquisition_bootstrap.py")
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class AcquisitionBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "snapshot"
        self.files = {"acquisition_tool/owner.sh": b"owner\n", "linux_idmap/probe.py": b"probe\n"}
        self.entries = {
            name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mode": 0o644}
            for name, data in self.files.items()
        }
        self.archive = self.root / "acquisition-owner-0.6.0.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, data in self.files.items():
                item = tarfile.TarInfo(name)
                item.size = len(data)
                item.mode = 0o644
                archive.addfile(item, io.BytesIO(data))
        self.record = {"files": {self.archive.name: hashlib.sha256(self.archive.read_bytes()).hexdigest()}}
        self.record["acquisition_source_files"] = self.entries
        self.issuer = self.root / "issuer.json"
        self.record_bytes = json.dumps(self.record).encode()

    def install(self):
        bootstrap.refresh(self.archive, self.snapshot, self.record, self.entries, self.issuer, self.record_bytes)

    def test_refresh_and_offline_verification_rejects_changed_or_extra_files(self):
        self.install()
        bootstrap.verify_snapshot(self.snapshot, self.entries)
        path = self.snapshot / "acquisition_tool/owner.sh"
        path.chmod(0o664)
        bootstrap.verify_snapshot(self.snapshot, self.entries)
        path.chmod(0o755)
        with self.assertRaisesRegex(ValueError, "executable bits"):
            bootstrap.verify_snapshot(self.snapshot, self.entries)
        path.chmod(0o644)
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "recorded bytes"):
            bootstrap.verify_snapshot(self.snapshot, self.entries)
        path.write_bytes(self.files["acquisition_tool/owner.sh"])
        (self.snapshot / "extra").write_text("extra")
        with self.assertRaisesRegex(ValueError, "inventory"):
            bootstrap.verify_snapshot(self.snapshot, self.entries)

    def test_bad_archive_cannot_modify_existing_snapshot(self):
        self.install()
        self.archive.write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "release pin"):
            self.install()
        bootstrap.verify_snapshot(self.snapshot, self.entries)

    def test_paths_and_symlinks_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid path"):
            bootstrap.inventory({"acquisition_source_files": {"../escape": {}}})
        with self.assertRaisesRegex(ValueError, "invalid path"):
            bootstrap.inventory({"acquisition_source_files": {".": {}}})
        self.install()
        target = self.snapshot / "acquisition_tool/owner.sh"
        target.unlink()
        target.symlink_to(self.archive)
        with self.assertRaisesRegex(ValueError, "non-regular"):
            bootstrap.verify_snapshot(self.snapshot, self.entries)

    def test_refresh_rejects_hardlinked_existing_files(self):
        self.install()
        target = self.snapshot / "acquisition_tool/owner.sh"
        target.unlink()
        other = self.root / "other"
        other.write_text("preserve")
        os.link(other, target)
        with self.assertRaisesRegex(ValueError, "hardlinked"):
            self.install()
        self.assertEqual(other.read_text(), "preserve")

    def test_refresh_preserves_modified_previous_release_and_record(self):
        self.install()
        owner = self.snapshot / "acquisition_tool/owner.sh"
        extra = self.snapshot / "extra"
        empty = self.snapshot / "local-directory"
        for mutation in ("bytes", "execute", "extra", "directory"):
            with self.subTest(mutation=mutation):
                owner.write_bytes(self.files["acquisition_tool/owner.sh"])
                owner.chmod(0o644)
                extra.unlink(missing_ok=True)
                if empty.exists():
                    empty.rmdir()
                if mutation == "bytes":
                    owner.write_bytes(b"local edit\n")
                elif mutation == "execute":
                    owner.chmod(0o755)
                elif mutation == "extra":
                    extra.write_bytes(b"local member\n")
                else:
                    empty.mkdir()
                original = owner.read_bytes(), owner.stat().st_mode
                with self.assertRaises(ValueError):
                    self.install()
                self.assertEqual(original, (owner.read_bytes(), owner.stat().st_mode))
                self.assertEqual(mutation == "extra", extra.exists())
                self.assertEqual(mutation == "directory", empty.is_dir())
                self.assertEqual(self.record_bytes, self.issuer.read_bytes())
                self.assertFalse(list(self.root.glob(".acquisition-*")))

    def previous_member(self):
        self.install()
        old = self.snapshot / "removed.sh"
        data = b"old-only\n"
        old.write_bytes(data)
        previous = json.loads(self.issuer.read_bytes())
        previous["acquisition_source_files"]["removed.sh"] = {
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "mode": 0o644,
        }
        previous_bytes = json.dumps(previous).encode()
        self.issuer.write_bytes(previous_bytes)
        return old, previous, previous_bytes

    def test_refresh_removes_only_a_previous_release_owned_member(self):
        old, _previous, _previous_bytes = self.previous_member()
        self.install()
        bootstrap.verify_snapshot(self.snapshot, self.entries)
        self.assertFalse(old.exists())
        self.assertEqual(self.record_bytes, self.issuer.read_bytes())
        self.assertFalse(list(self.root.glob(".acquisition-*")))

    def test_record_publication_failure_restores_previous_snapshot_and_record(self):
        old, previous, previous_bytes = self.previous_member()
        with (
            patch.object(Path, "replace", side_effect=OSError("record publication failed")),
            self.assertRaisesRegex(OSError, "record publication failed"),
        ):
            self.install()
        bootstrap.verify_snapshot(self.snapshot, previous["acquisition_source_files"])
        self.assertEqual(b"old-only\n", old.read_bytes())
        self.assertEqual(previous_bytes, self.issuer.read_bytes())
        self.assertFalse(list(self.root.glob(".acquisition-*")))

    def test_backup_cleanup_failure_reports_the_published_replacement(self):
        _old, previous, _previous_bytes = self.previous_member()
        with (
            patch.object(bootstrap.shutil, "rmtree", side_effect=OSError("backup cleanup failed")),
            self.assertRaisesRegex(OSError, "replacement published; cannot remove previous snapshot") as raised,
        ):
            self.install()
        self.assertEqual("backup cleanup failed", str(raised.exception.__cause__))
        bootstrap.verify_snapshot(self.snapshot, self.entries)
        self.assertEqual(self.record_bytes, self.issuer.read_bytes())
        backups = list(self.root.glob(".acquisition-previous-*"))
        self.assertEqual(1, len(backups))
        bootstrap.verify_snapshot(backups[0], previous["acquisition_source_files"])


if __name__ == "__main__":
    unittest.main()
