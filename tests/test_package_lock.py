"""Rejected package-lock application must preserve both installed outputs."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
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
        self.old_modes = {name: (self.mounts["/output"] / name).stat().st_mode & 0o777
                          for name in self.old}

    def apply(self):
        # Map the CLI's fixed container mounts into this isolated fixture.
        with patch.object(package_lock, "Path", side_effect=lambda value: self.mounts.get(value, Path(value))):
            package_lock.apply()

    def assert_outputs_unchanged(self):
        for name, body in self.old.items():
            self.assertEqual((self.mounts["/output"] / name).read_bytes(), body)
            self.assertEqual((self.mounts["/output"] / name).stat().st_mode & 0o777, self.old_modes[name])
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
        replace = os.replace

        def assert_prepared_before_install(source, destination):
            if Path(source).name == "candidate-inference.lock":
                work = Path(source).parent
                self.assertEqual((work / "candidate-advisories.json").read_bytes(), self.report)
                for name, body in self.old.items():
                    self.assertEqual((work / ("previous-" + name)).read_bytes(), body)
                    self.assertEqual((work / ("previous-" + name)).stat().st_mode & 0o777, self.old_modes[name])
            return replace(source, destination)

        with patch.object(package_lock.os, "replace", side_effect=assert_prepared_before_install):
            self.apply()
        self.assertEqual((self.mounts["/output"] / "inference.lock").read_bytes(), self.lock)
        self.assertEqual((self.mounts["/output"] / "advisories.json").read_bytes(), self.report)
        self.assertEqual(sorted(path.name for path in self.mounts["/output"].iterdir()), sorted(self.old))
        for name in self.old:
            self.assertEqual((self.mounts["/output"] / name).stat().st_mode & 0o777, 0o644)

    def fail_candidate_report(self, source, destination):
        if Path(source).name == "candidate-advisories.json":
            raise OSError("injected second output installation failure")
        return self.replace(source, destination)

    def test_second_install_failure_restores_both_bytes_and_modes(self):
        self.replace = os.replace
        self.old_modes.update({"inference.lock": 0o640, "advisories.json": 0o600})
        for name, mode in self.old_modes.items():
            (self.mounts["/output"] / name).chmod(mode)
        with patch.object(package_lock.os, "replace", side_effect=self.fail_candidate_report):
            with self.assertRaisesRegex(OSError, "second output installation"):
                self.apply()
        self.assert_outputs_unchanged()

    def test_first_install_failure_preserves_predecessors(self):
        with patch.object(package_lock.os, "replace", side_effect=OSError("first installation failure")):
            with self.assertRaisesRegex(OSError, "first installation"):
                self.apply()
        self.assert_outputs_unchanged()

    def test_candidate_preparation_failure_never_replaces_outputs(self):
        write_bytes = Path.write_bytes

        def fail_report(path, data):
            if path.name == "candidate-advisories.json":
                raise OSError("candidate preparation failure")
            return write_bytes(path, data)

        with patch.object(Path, "write_bytes", autospec=True, side_effect=fail_report), \
             patch.object(package_lock.os, "replace") as replace:
            with self.assertRaisesRegex(OSError, "candidate preparation"):
                self.apply()
            replace.assert_not_called()
        self.assert_outputs_unchanged()

    def test_failed_apply_restores_prior_absence_for_new_destinations(self):
        self.replace = os.replace
        for existing in ((), ("inference.lock",), ("advisories.json",)):
            with self.subTest(existing=existing):
                for name, body in self.old.items():
                    path = self.mounts["/output"] / name
                    path.unlink(missing_ok=True)
                    if name in existing:
                        path.write_bytes(body)
                with patch.object(package_lock.os, "replace", side_effect=self.fail_candidate_report):
                    with self.assertRaisesRegex(OSError, "second output installation"):
                        self.apply()
                self.assertEqual({path.name for path in self.mounts["/output"].iterdir()}, set(existing))
                for name in existing:
                    self.assertEqual((self.mounts["/output"] / name).read_bytes(), self.old[name])

    def test_interruption_after_second_rename_restores_consumed_candidates(self):
        replace = os.replace

        def interrupt_after_install(source, destination):
            replace(source, destination)
            if Path(source).name == "candidate-advisories.json":
                raise KeyboardInterrupt("interrupted after publication")

        with patch.object(package_lock.os, "replace", side_effect=interrupt_after_install):
            with self.assertRaisesRegex(KeyboardInterrupt, "after publication"):
                self.apply()
        self.assert_outputs_unchanged()

    def test_failed_recovery_preserves_evidence_and_original_failure(self):
        replace = os.replace
        failure = OSError("original publication failure")

        def fail_publish_and_restore(source, destination):
            if Path(source).name == "candidate-advisories.json":
                raise failure
            if Path(source).name == "previous-inference.lock":
                raise OSError("predecessor restoration failure")
            return replace(source, destination)

        with patch.object(package_lock.os, "replace", side_effect=fail_publish_and_restore):
            with self.assertRaises(OSError) as caught:
                self.apply()
        self.assertIs(caught.exception, failure)
        self.assertIn("Cannot confirm predecessor restoration for inference.lock", "\n".join(failure.__notes__))
        work, = self.mounts["/output"].glob(".lock-apply.*")
        self.assertEqual(work.stat().st_mode & 0o777, 0o700)
        self.assertEqual((work / "previous-inference.lock").read_bytes(), self.old["inference.lock"])
        self.assertEqual((self.mounts["/output"] / "inference.lock").read_bytes(), self.lock)
        self.assertEqual((self.mounts["/output"] / "advisories.json").read_bytes(), self.old["advisories.json"])

    def test_candidate_inspection_failure_preserves_primary_error_and_recovers_other_member(self):
        replace = os.replace
        exists = Path.exists
        failure = OSError("original publication failure")
        publication_failed = False

        def fail_after_report(source, destination):
            nonlocal publication_failed
            replace(source, destination)
            if Path(source).name == "candidate-advisories.json":
                publication_failed = True
                raise failure

        def fail_report_inspection(path):
            if publication_failed and path.name == "candidate-advisories.json":
                raise OSError("candidate inspection I/O failure")
            return exists(path)

        with patch.object(package_lock.os, "replace", side_effect=fail_after_report), \
             patch.object(Path, "exists", autospec=True, side_effect=fail_report_inspection):
            with self.assertRaises(OSError) as caught:
                self.apply()
        self.assertIs(caught.exception, failure)
        self.assertIn("candidate inspection I/O failure", "\n".join(failure.__notes__))
        self.assertEqual((self.mounts["/output"] / "inference.lock").read_bytes(), self.old["inference.lock"])
        self.assertEqual((self.mounts["/output"] / "advisories.json").read_bytes(), self.report)
        work, = self.mounts["/output"].glob(".lock-apply.*")
        self.assertEqual((work / "previous-advisories.json").read_bytes(), self.old["advisories.json"])

    def test_missing_expected_backup_is_uncertain_not_prior_absence(self):
        replace = os.replace
        failure = OSError("original publication failure")

        def discard_backup_then_fail(source, destination):
            if Path(source).name == "candidate-advisories.json":
                (Path(source).parent / "previous-inference.lock").unlink()
                raise failure
            return replace(source, destination)

        with patch.object(package_lock.os, "replace", side_effect=discard_backup_then_fail):
            with self.assertRaises(OSError) as caught:
                self.apply()
        self.assertIs(caught.exception, failure)
        self.assertIn("Cannot confirm predecessor restoration for inference.lock", "\n".join(failure.__notes__))
        self.assertEqual((self.mounts["/output"] / "inference.lock").read_bytes(), self.lock)
        self.assertEqual((self.mounts["/output"] / "advisories.json").read_bytes(), self.old["advisories.json"])
        self.assertEqual(len(list(self.mounts["/output"].glob(".lock-apply.*"))), 1)

    def test_cleanup_failure_after_recovery_reports_restored_outputs(self):
        self.replace = os.replace
        with patch.object(package_lock.os, "replace", side_effect=self.fail_candidate_report), \
             patch.object(package_lock.shutil, "rmtree", side_effect=OSError("cleanup failure")):
            with self.assertRaisesRegex(OSError, "second output installation") as caught:
                self.apply()
        self.assertIn("Predecessor outputs were restored", "\n".join(caught.exception.__notes__))
        for name, body in self.old.items():
            self.assertEqual((self.mounts["/output"] / name).read_bytes(), body)
        self.assertEqual(len(list(self.mounts["/output"].glob(".lock-apply.*"))), 1)

    def test_cleanup_failure_after_install_reports_published_pair(self):
        with patch.object(package_lock.shutil, "rmtree", side_effect=OSError("cleanup failure")):
            with self.assertRaisesRegex(OSError, "cleanup failure") as caught:
                self.apply()
        self.assertIn("Both reviewed outputs were installed", "\n".join(caught.exception.__notes__))
        self.assertEqual((self.mounts["/output"] / "inference.lock").read_bytes(), self.lock)
        self.assertEqual((self.mounts["/output"] / "advisories.json").read_bytes(), self.report)
        self.assertEqual(len(list(self.mounts["/output"].glob(".lock-apply.*"))), 1)


if __name__ == "__main__":
    unittest.main()
