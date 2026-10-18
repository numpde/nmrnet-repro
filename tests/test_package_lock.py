"""Rejected package-lock application must preserve both installed outputs."""

from tools.container_guard import require_container

require_container()

import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import package_lock


class PackageLockAdvisoryTests(unittest.TestCase):
    def test_request_failure_preserves_exception_and_names_exact_package(self):
        failure = OSError("fixture network failure")
        with (
            patch.object(package_lock.urllib.request, "urlopen", side_effect=failure) as request,
            self.assertRaises(OSError) as caught,
        ):
            package_lock.advisory(("fixture-package", "2.3.4"))
        self.assertIs(failure, caught.exception)
        self.assertIn("PyPI advisory request failed for fixture-package==2.3.4.", caught.exception.__notes__)
        request.assert_called_once_with("https://pypi.org/pypi/fixture-package/2.3.4/json", timeout=60)

    def test_invalid_json_and_missing_field_name_exact_package(self):
        for body, error_type in ((b"{", json.JSONDecodeError), (b"{}", KeyError)):
            with self.subTest(body=body):
                with (
                    patch.object(package_lock.urllib.request, "urlopen", return_value=io.BytesIO(body)),
                    self.assertRaises(error_type) as caught,
                ):
                    package_lock.advisory(("fixture-package", "2.3.4"))
                self.assertIn("Invalid PyPI advisory metadata for fixture-package==2.3.4.", caught.exception.__notes__)


class PackageLockStageTests(unittest.TestCase):
    """Synthetic resolver output exercises today's producer, not historical raw."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = self.root / "requirements"
        self.output = self.root / "output"
        self.inputs.mkdir()
        self.output.mkdir()
        self.digest = "a" * 64
        self.url = "https://fixture.test/cpu/torch-2.13.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl"
        self.reference = self.url + "#sha256=" + self.digest
        self.intent = self.inputs / "inference.in"
        self.intent.write_text("--index-url https://pypi.org/simple\nexample==1.0\ntorch @ " + self.reference + "\n")
        (self.inputs / "build.in").write_text("# no additional direct packages\n")
        (self.inputs / "unicore-source.json").write_text('{"fixture_source":true}\n')
        self.example = "example==1.0 \\\n    --hash=sha256:" + "b" * 64 + "\n    # via fixture\n"
        self.torch = "torch @ " + self.reference + " \\\n    --hash=sha256:" + self.digest + "\n    # via -r input/inference.in\n"
        self.resolved = self.example + self.torch
        self.mounts = {"/input": self.inputs, "/output": self.output, "/input/inference.in": self.intent}
        self.expected_packages = [
            {"name": "example", "version": "1.0", "url": "https://fixture.test/advisory/example", "vulnerabilities": []},
            {"name": "torch", "version": "2.13.0+cpu", "url": "https://fixture.test/advisory/torch", "vulnerabilities": []},
        ]

    def stage(self, resolved):
        def resolve(command, **keywords):
            self.assertEqual(
                [
                    "uv",
                    "pip",
                    "compile",
                    "/input/inference.in",
                    "/input/build.in",
                    "--python-version",
                    "3.12.12",
                    "--python-platform",
                    "x86_64-manylinux_2_28",
                    "--generate-hashes",
                    "--no-build",
                    "--no-emit-index-url",
                    "--no-emit-find-links",
                    "--no-header",
                    "--output-file",
                    str(self.output / "resolved.raw"),
                ],
                command,
            )
            self.assertEqual({"check": True, "stdout": package_lock.subprocess.DEVNULL}, keywords)
            (self.output / "resolved.raw").write_text(resolved)

        def advisory(requirement):
            name, version = requirement
            record = next(record for record in self.expected_packages if record["name"] == name)
            self.assertEqual(record["version"], version)
            return record

        with (
            patch.object(package_lock, "Path", side_effect=self.mounts.__getitem__),
            patch.object(package_lock.subprocess, "run", side_effect=resolve) as compile_lock,
            patch.object(package_lock, "advisory", side_effect=advisory) as check_advisory,
        ):
            self.check_advisory = check_advisory
            package_lock.stage("inference")
            self.assertEqual(1, compile_lock.call_count)
            self.assertEqual(2, check_advisory.call_count)

    def test_actual_stage_emits_headerless_exact_pins_and_binds_real_output_and_inputs(self):
        expected = self.example + "torch==2.13.0+cpu \\\n    --hash=sha256:" + self.digest + "\n    # via -r input/inference.in\n"
        for resolved in (self.resolved, self.resolved.replace("%2Bcpu", "+cpu")):
            with self.subTest(resolved=resolved):
                self.stage(resolved)
                lock = (self.output / "inference.lock").read_bytes()
                self.assertEqual(expected.encode(), lock)
                report = json.loads((self.output / "advisories.json").read_bytes())
                self.assertEqual(hashlib.sha256(lock).hexdigest(), report["lock_sha256"])
                self.assertEqual(
                    {
                        name: hashlib.sha256((self.inputs / name).read_bytes()).hexdigest()
                        for name in ("inference.in", "build.in", "unicore-source.json")
                    },
                    report["inputs_sha256"],
                )
                self.assertEqual(self.expected_packages, report["packages"])
                self.assertFalse((self.output / "resolved.raw").exists())

    def test_unsupported_torch_output_fails_before_advisories_or_publication(self):
        hash_line = "    --hash=sha256:" + self.digest + "\n"
        cases = (
            self.example,
            self.resolved.replace(hash_line, ""),
            self.resolved.replace(hash_line, hash_line * 2),
            self.resolved.replace(hash_line, hash_line.rstrip() + " \\\n" + hash_line),
            self.resolved + self.torch,
            self.resolved + self.torch.replace("torch @ ", "TORCH @ "),
            self.resolved.replace("#sha256=" + self.digest, "#sha256=" + "c" * 64),
            self.resolved.replace(hash_line, "    --hash=sha256:" + "c" * 64 + "\n"),
            self.resolved.replace("cp312-cp312", "cp311-cp311"),
            self.resolved.replace("2.13.0%2Bcpu", "2.14.0%2Bcpu"),
            self.resolved.replace("fixture.test/cpu/", "fixture.test/cp%75/"),
            self.resolved.replace("%2Bcpu", "%2bcpu"),
            self.resolved.replace("\n", "\r\n"),
            self.resolved.rstrip("\n"),
            self.example + "torch==2.13.0+cpu \\\n" + hash_line.rstrip() + " \\\n" + hash_line,
        )
        for resolved in cases:
            with self.subTest(resolved=resolved):
                with self.assertRaisesRegex(ValueError, "Cannot stage lock"):
                    self.stage(resolved)
                self.check_advisory.assert_not_called()
                self.assertFalse((self.output / "inference.lock").exists())
                self.assertFalse((self.output / "advisories.json").exists())

    def test_missing_duplicated_or_incompatible_declared_torch_url_fails(self):
        valid = self.intent.read_text()
        for declaration in (
            valid.replace("torch @ " + self.reference + "\n", ""),
            valid + "torch @ " + self.reference + "\n",
            valid.replace("cp312-cp312", "cp311-cp311"),
        ):
            self.intent.write_text(declaration)
            with self.subTest(declaration=declaration):
                with (
                    patch.object(package_lock, "Path", side_effect=self.mounts.__getitem__),
                    patch.object(package_lock.subprocess, "run") as compile_lock,
                    patch.object(package_lock, "advisory") as check_advisory,
                    self.assertRaisesRegex(ValueError, "inference.in|declared Torch"),
                ):
                    package_lock.stage("inference")
                compile_lock.assert_not_called()
                check_advisory.assert_not_called()
                self.assertFalse((self.output / "inference.lock").exists())

    def test_torch_url_userinfo_rejects_before_resolver_without_disclosure(self):
        secret = "fixture-secret"
        credential_url = self.reference.replace("https://fixture.test/", f"https://user:{secret}@fixture.test/")
        self.intent.write_text("--index-url https://pypi.org/simple\nexample==1.0\ntorch @ " + credential_url + "\n")
        with (
            patch.object(package_lock, "Path", side_effect=self.mounts.__getitem__),
            patch.object(package_lock.subprocess, "run") as compile_lock,
            self.assertRaisesRegex(ValueError, "must not contain user information") as failure,
        ):
            package_lock.stage("inference")
        compile_lock.assert_not_called()
        self.assertNotIn(secret, str(failure.exception))
        self.assertFalse((self.output / "inference.lock").exists())
        self.assertFalse((self.output / "advisories.json").exists())

    def test_input_drift_during_resolution_fails_before_publication(self):
        write_text = Path.write_text

        def change_input_after_resolution(path, text, *arguments, **keywords):
            result = write_text(path, text, *arguments, **keywords)
            if path == self.output / "resolved.raw":
                write_text(self.inputs / "build.in", "# changed during resolution\n")
            return result

        with (
            patch.object(Path, "write_text", autospec=True, side_effect=change_input_after_resolution),
            self.assertRaisesRegex(ValueError, "inputs changed during resolution"),
        ):
            self.stage(self.resolved)
        self.assertFalse((self.output / "inference.lock").exists())
        self.assertFalse((self.output / "advisories.json").exists())


class PreparationLockTests(unittest.TestCase):
    """Independent wheel-only profiles retain separate reviewed output pairs."""

    def test_preparation_stage_and_apply_preserve_inference_and_reject_changed_intent(self):
        self.exercise_profile("preparation", "rdkit==2026.9.1")

    def test_provider_core_stage_and_apply_preserve_inference_and_reject_changed_intent(self):
        self.exercise_profile("provider-core", "httpx==0.28.1")

    def exercise_profile(self, profile, dependency):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mounts = {name: root / name.lstrip("/") for name in ("/input", "/output", "/stage")}
            for directory in mounts.values():
                directory.mkdir()
            intent = (dependency + "\n").encode()
            (mounts["/input"] / f"{profile}.in").write_bytes(intent)
            lock = dependency + " \\\n    --hash=sha256:" + "a" * 64 + "\n"

            def resolve(command, **keywords):
                self.assertEqual(["uv", "pip", "compile", f"/input/{profile}.in"], command[:4])
                self.assertNotIn("/input/inference.in", command)
                self.assertIn("--no-build", command)
                (mounts["/output"] / "resolved.raw").write_text(lock)

            with (
                patch.object(package_lock, "Path", side_effect=mounts.__getitem__),
                patch.object(package_lock.subprocess, "run", side_effect=resolve),
                patch.object(package_lock, "advisory", return_value={"vulnerabilities": []}),
                patch.object(package_lock, "normalize_torch_requirement") as normalize,
            ):
                package_lock.stage(profile)
                normalize.assert_not_called()
            report = json.loads((mounts["/output"] / f"{profile}-advisories.json").read_bytes())
            self.assertEqual({f"{profile}.in": hashlib.sha256(intent).hexdigest()}, report["inputs_sha256"])
            self.assertEqual(hashlib.sha256(lock.encode()).hexdigest(), report["lock_sha256"])
            for name in (f"{profile}.lock", f"{profile}-advisories.json"):
                (mounts["/output"] / name).rename(mounts["/stage"] / name)
            (mounts["/output"] / "inference.lock").write_bytes(b"qualified inference")
            (mounts["/output"] / "advisories.json").write_bytes(b"qualified advisories")
            if profile == "provider-core":
                (mounts["/output"] / "preparation.lock").write_bytes(b"qualified preparation")
                (mounts["/output"] / "preparation-advisories.json").write_bytes(b"qualified preparation advisories")

            def mounted_path(value):
                if value in mounts:
                    return mounts[value]
                return Path(value)

            with patch.object(package_lock, "Path", side_effect=mounted_path):
                (mounts["/input"] / f"{profile}.in").write_bytes(b"changed\n")
                with self.assertRaisesRegex(ValueError, "inputs changed"):
                    package_lock.apply(profile)
                self.assertFalse((mounts["/output"] / f"{profile}.lock").exists())
                (mounts["/input"] / f"{profile}.in").write_bytes(intent)
                report_path = mounts["/stage"] / f"{profile}-advisories.json"
                report_bytes = report_path.read_bytes()
                report["inputs_sha256"] = {"inference.in": hashlib.sha256(intent).hexdigest()}
                report_path.write_text(json.dumps(report))
                with self.assertRaisesRegex(ValueError, "inputs changed"):
                    package_lock.apply(profile)
                self.assertFalse((mounts["/output"] / f"{profile}.lock").exists())
                report_path.write_bytes(report_bytes)
                package_lock.apply(profile)
            self.assertEqual(lock.encode(), (mounts["/output"] / f"{profile}.lock").read_bytes())
            self.assertEqual(b"qualified inference", (mounts["/output"] / "inference.lock").read_bytes())
            self.assertEqual(b"qualified advisories", (mounts["/output"] / "advisories.json").read_bytes())
            if profile == "provider-core":
                self.assertEqual(b"qualified preparation", (mounts["/output"] / "preparation.lock").read_bytes())
                self.assertEqual(
                    b"qualified preparation advisories", (mounts["/output"] / "preparation-advisories.json").read_bytes()
                )

    def test_unknown_profile_rejects_before_reading_or_resolving(self):
        with patch.object(package_lock, "Path") as paths, patch.object(package_lock.subprocess, "run") as run:
            for action in (package_lock.stage, package_lock.apply):
                with self.assertRaisesRegex(ValueError, "profile"):
                    action("../inference")
            paths.assert_not_called()
            run.assert_not_called()


class PackageLockApplyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.mounts = {name: self.root / name.lstrip("/") for name in ("/input", "/stage", "/output")}
        for directory in self.mounts.values():
            directory.mkdir()
        inputs = {}
        for name in package_lock.INPUTS:
            body = f"original {name}".encode()
            (self.mounts["/input"] / name).write_bytes(body)
            inputs[name] = hashlib.sha256(body).hexdigest()
        self.lock = b"candidate==1.0\n"
        self.report = json.dumps(
            {
                "lock_sha256": hashlib.sha256(self.lock).hexdigest(),
                "inputs_sha256": inputs,
            }
        ).encode()
        self.old = {"inference.lock": b"installed lock", "advisories.json": b"installed report"}
        for name, body in (("inference.lock", self.lock), ("advisories.json", self.report)):
            (self.mounts["/stage"] / name).write_bytes(body)
            (self.mounts["/output"] / name).write_bytes(self.old[name])
        self.old_modes = {name: (self.mounts["/output"] / name).stat().st_mode & 0o777 for name in self.old}

    def apply(self):
        # Map the CLI's fixed container mounts into this isolated fixture.
        with patch.object(
            package_lock,
            "Path",
            side_effect=lambda value: (
                # silent-defaults: allow-lambda-body -- Redirect /input, /stage and /output into this test; generated temporary paths already point there.
                self.mounts.get(value, Path(value))
            ),
        ):
            package_lock.apply("inference")

    def assert_outputs_unchanged(self):
        for name, body in self.old.items():
            self.assertEqual((self.mounts["/output"] / name).read_bytes(), body)
            self.assertEqual((self.mounts["/output"] / name).stat().st_mode & 0o777, self.old_modes[name])
        self.assertEqual(sorted(path.name for path in self.mounts["/output"].iterdir()), sorted(self.old))

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
        self.assertEqual((self.mounts["/output"] / "inference.lock").read_bytes(), self.old["inference.lock"])
        self.assertEqual([], list(report.iterdir()))

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

        with patch.object(os, "replace", side_effect=assert_prepared_before_install):
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
        with (
            patch.object(os, "replace", side_effect=self.fail_candidate_report),
            self.assertRaisesRegex(OSError, "second output installation") as caught,
        ):
            self.apply()
        self.assertIn(
            "Previous output state was restored; the reviewed pair was not applied.",
            caught.exception.__notes__,
        )
        self.assert_outputs_unchanged()

    def test_first_install_failure_preserves_predecessors(self):
        with (
            patch.object(os, "replace", side_effect=OSError("first installation failure")),
            self.assertRaisesRegex(OSError, "first installation"),
        ):
            self.apply()
        self.assert_outputs_unchanged()

    def test_candidate_preparation_failure_never_replaces_outputs(self):
        write_bytes = Path.write_bytes

        def fail_report(path, data):
            if path.name == "candidate-advisories.json":
                raise OSError("candidate preparation failure")
            return write_bytes(path, data)

        with patch.object(Path, "write_bytes", autospec=True, side_effect=fail_report), patch.object(os, "replace") as replace:
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
                with (
                    patch.object(os, "replace", side_effect=self.fail_candidate_report),
                    self.assertRaisesRegex(OSError, "second output installation"),
                ):
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

        with (
            patch.object(os, "replace", side_effect=interrupt_after_install),
            self.assertRaisesRegex(KeyboardInterrupt, "after publication"),
        ):
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

        with patch.object(os, "replace", side_effect=fail_publish_and_restore), self.assertRaises(OSError) as caught:
            self.apply()
        self.assertIs(caught.exception, failure)
        self.assertIn("Cannot confirm predecessor restoration for inference.lock", "\n".join(failure.__notes__))
        self.assertNotIn("Previous output state was restored", "\n".join(failure.__notes__))
        (work,) = self.mounts["/output"].glob(".lock-apply.*")
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

        with (
            patch.object(os, "replace", side_effect=fail_after_report),
            patch.object(Path, "exists", autospec=True, side_effect=fail_report_inspection),
            self.assertRaises(OSError) as caught,
        ):
            self.apply()
        self.assertIs(caught.exception, failure)
        self.assertIn("candidate inspection I/O failure", "\n".join(failure.__notes__))
        self.assertEqual((self.mounts["/output"] / "inference.lock").read_bytes(), self.old["inference.lock"])
        self.assertEqual((self.mounts["/output"] / "advisories.json").read_bytes(), self.report)
        (work,) = self.mounts["/output"].glob(".lock-apply.*")
        self.assertEqual((work / "previous-advisories.json").read_bytes(), self.old["advisories.json"])

    def test_missing_expected_backup_is_uncertain_not_prior_absence(self):
        replace = os.replace
        failure = OSError("original publication failure")

        def discard_backup_then_fail(source, destination):
            if Path(source).name == "candidate-advisories.json":
                (Path(source).parent / "previous-inference.lock").unlink()
                raise failure
            return replace(source, destination)

        with patch.object(os, "replace", side_effect=discard_backup_then_fail), self.assertRaises(OSError) as caught:
            self.apply()
        self.assertIs(caught.exception, failure)
        self.assertIn("Cannot confirm predecessor restoration for inference.lock", "\n".join(failure.__notes__))
        self.assertEqual((self.mounts["/output"] / "inference.lock").read_bytes(), self.lock)
        self.assertEqual((self.mounts["/output"] / "advisories.json").read_bytes(), self.old["advisories.json"])
        self.assertEqual(1, len(list(self.mounts["/output"].glob(".lock-apply.*"))))

    def test_cleanup_failure_after_recovery_reports_restored_outputs(self):
        self.replace = os.replace
        with (
            patch.object(os, "replace", side_effect=self.fail_candidate_report),
            patch.object(package_lock.shutil, "rmtree", side_effect=OSError("cleanup failure")),
            self.assertRaisesRegex(OSError, "second output installation") as caught,
        ):
            self.apply()
        self.assertIn("Predecessor outputs were restored", "\n".join(caught.exception.__notes__))
        self.assertNotIn("Previous output state was restored", "\n".join(caught.exception.__notes__))
        for name, body in self.old.items():
            self.assertEqual((self.mounts["/output"] / name).read_bytes(), body)
        self.assertEqual(1, len(list(self.mounts["/output"].glob(".lock-apply.*"))))

    def test_cleanup_failure_after_install_reports_published_pair(self):
        with (
            patch.object(package_lock.shutil, "rmtree", side_effect=OSError("cleanup failure")),
            self.assertRaisesRegex(OSError, "cleanup failure") as caught,
        ):
            self.apply()
        self.assertIn("Both reviewed outputs were installed", "\n".join(caught.exception.__notes__))
        self.assertEqual((self.mounts["/output"] / "inference.lock").read_bytes(), self.lock)
        self.assertEqual((self.mounts["/output"] / "advisories.json").read_bytes(), self.report)
        self.assertEqual(1, len(list(self.mounts["/output"].glob(".lock-apply.*"))))


if __name__ == "__main__":
    unittest.main()
