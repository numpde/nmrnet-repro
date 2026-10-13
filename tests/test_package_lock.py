"""Rejected package-lock application must preserve both installed outputs."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import package_lock


class PackageLockApplyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mounts = {name: self.root / name.lstrip("/")
                       for name in ("/input", "/stage", "/output")}
        for directory in self.mounts.values():
            directory.mkdir()
        inputs = {}
        for name in package_lock.INPUTS:
            body = f"original {name}".encode()
            (self.mounts["/input"] / name).write_bytes(body)
            inputs[name] = hashlib.sha256(body).hexdigest()
        self.lock = b"candidate==1.0\n"
        self.report = json.dumps({
            "lock_sha256": hashlib.sha256(self.lock).hexdigest(),
            "inputs_sha256": inputs,
        }).encode()
        self.old = {"inference.lock": b"installed lock", "advisories.json": b"installed report"}
        for name, body in (("inference.lock", self.lock), ("advisories.json", self.report)):
            (self.mounts["/stage"] / name).write_bytes(body)
            (self.mounts["/output"] / name).write_bytes(self.old[name])

    def apply(self):
        # Map the CLI's fixed container mounts into this isolated fixture.
        mkstemp = tempfile.mkstemp
        with patch.object(package_lock, "Path", side_effect=lambda value: self.mounts.get(value, Path(value))), \
             patch.object(package_lock.tempfile, "mkstemp", side_effect=lambda **kwargs: mkstemp(
                 **(kwargs | {"dir": self.mounts["/output"]}))):
            package_lock.apply()

    def assert_outputs_unchanged(self):
        for name, body in self.old.items():
            self.assertEqual((self.mounts["/output"] / name).read_bytes(), body)
        self.assertEqual(sorted(path.name for path in self.mounts["/output"].iterdir()),
                         sorted(self.old))

    def test_report_symlink_rejects_before_replacing_lock(self):
        report = self.mounts["/output"] / "advisories.json"
        report.unlink()
        sentinel = self.root / "report-target"
        sentinel.write_bytes(self.old["advisories.json"])
        report.symlink_to(sentinel)
        with self.assertRaisesRegex(ValueError, "advisories.json"):
            self.apply()
        self.assertTrue(report.is_symlink())
        self.assert_outputs_unchanged()

    def test_report_directory_rejects_before_replacing_lock(self):
        report = self.mounts["/output"] / "advisories.json"
        report.unlink()
        report.mkdir()
        with self.assertRaisesRegex(ValueError, "advisories.json"):
            self.apply()
        self.assertEqual((self.mounts["/output"] / "inference.lock").read_bytes(),
                         self.old["inference.lock"])
        self.assertEqual(list(report.iterdir()), [])

    def test_input_drift_rejects_without_writes(self):
        (self.mounts["/input"] / "inference.in").write_bytes(b"changed input")
        with self.assertRaisesRegex(ValueError, "inputs changed"):
            self.apply()
        self.assert_outputs_unchanged()

    def test_corrupt_staged_lock_rejects_without_writes(self):
        (self.mounts["/stage"] / "inference.lock").write_bytes(b"modified lock")
        with self.assertRaisesRegex(ValueError, "staged bytes differ"):
            self.apply()
        self.assert_outputs_unchanged()

    def test_valid_stage_applies_both_exact_outputs(self):
        self.apply()
        self.assertEqual((self.mounts["/output"] / "inference.lock").read_bytes(), self.lock)
        self.assertEqual((self.mounts["/output"] / "advisories.json").read_bytes(), self.report)


if __name__ == "__main__":
    unittest.main()
