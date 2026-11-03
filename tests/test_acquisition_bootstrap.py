"""The cold acquisition closure must match its pinned release inventory."""

import hashlib
import importlib.util
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path

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
        self.entries = {name: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mode": 0o644}
                        for name, data in self.files.items()}
        self.archive = self.root / "acquisition-owner-0.6.0.tar.gz"
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, data in self.files.items():
                item = tarfile.TarInfo(name)
                item.size = len(data)
                item.mode = 0o644
                archive.addfile(item, io.BytesIO(data))
        self.record = {"files": {self.archive.name: hashlib.sha256(self.archive.read_bytes()).hexdigest()}}

    def test_refresh_and_offline_verification_rejects_changed_or_extra_files(self):
        bootstrap.refresh(self.archive, self.snapshot, self.record, self.entries)
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
        bootstrap.refresh(self.archive, self.snapshot, self.record, self.entries)
        self.archive.write_bytes(b"bad")
        with self.assertRaisesRegex(ValueError, "release pin"):
            bootstrap.refresh(self.archive, self.snapshot, self.record, self.entries)
        bootstrap.verify_snapshot(self.snapshot, self.entries)

    def test_paths_and_symlinks_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid path"):
            bootstrap.inventory({"acquisition_source_files": {"../escape": {}}})
        with self.assertRaisesRegex(ValueError, "invalid path"):
            bootstrap.inventory({"acquisition_source_files": {".": {}}})
        bootstrap.refresh(self.archive, self.snapshot, self.record, self.entries)
        target = self.snapshot / "acquisition_tool/owner.sh"
        target.unlink()
        target.symlink_to(self.archive)
        with self.assertRaisesRegex(ValueError, "non-regular"):
            bootstrap.verify_snapshot(self.snapshot, self.entries)

    def test_refresh_rejects_hardlinked_existing_files(self):
        bootstrap.refresh(self.archive, self.snapshot, self.record, self.entries)
        target = self.snapshot / "acquisition_tool/owner.sh"
        target.unlink()
        other = self.root / "other"
        other.write_text("preserve")
        os.link(other, target)
        with self.assertRaisesRegex(ValueError, "hardlinked"):
            bootstrap.refresh(self.archive, self.snapshot, self.record, self.entries)
        self.assertEqual(other.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
