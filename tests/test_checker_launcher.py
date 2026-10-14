"""Exercise checker capture, publication and grants with a contained finite engine."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from tests.test_checker import CheckerDependencyFixture

# Both maintained test lanes set /workspace as the working directory; fixture
# source paths use that explicit runner contract rather than this file's layout.
ROOT = Path.cwd()


class CheckerLauncherTests(CheckerDependencyFixture):
    def setUp(self):
        super().setUp()
        for directory in ("tools", "requirements", "containers/checker", "state", "bin"):
            (self.root / directory).mkdir(parents=True)
        for name in ("checker.py", "checker.sh", "artifacts.py", "container_guard.py", "docker.sh"):
            shutil.copyfile(ROOT / "tools" / name, self.root / "tools" / name)
        for name in ("Dockerfile", "Dockerfile.dockerignore"):
            (self.root / "containers/checker" / name).write_text("fixture recipe\n")
        self.document["base"] = (
            "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
        )
        self.write()
        self.complete()
        shutil.copyfile(self.path, self.root / "requirements/checker-dependencies.json")
        (self.root / ".work/checker").mkdir(parents=True)
        self.directory.rename(self.root / ".work/checker/dependencies")
        self.directory = self.root / ".work/checker/dependencies"
        (self.root / "bin/docker").symlink_to(ROOT / "tests/test_checker_engine.py")

    def launch(self, action, *, preview=False, additions=None):
        environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PATH": str(self.root / "bin") + ":" + str(Path(sys.executable).parent) + ":" + os.defpath,
            "CHECKER_FIXTURE_STATE": str(self.root / "state"),
            "NMRNET_WLAN_INTERFACE_INPUT": "lo",
            "DOCKER_HOST": "ambient-sentinel",
            "DOCKER_CONTEXT": "ambient-sentinel",
            "BUILDX_BUILDER": "ambient-sentinel",
            "BUILDX_CONFIG": "ambient-sentinel",
            "DOCKER_BUILDKIT": "0",
            **(additions or {}),
        }
        command = ["bash", str(self.root / "tools/checker.sh")]
        if preview:
            command.append("--print")
        return subprocess.run([*command, action], env=environment, text=True, capture_output=True, timeout=10, check=False)

    def calls(self):
        return [json.loads(line) for line in (self.root / "state/calls.jsonl").read_text().splitlines()]

    def test_offline_build_captures_consumed_bytes_records_observed_id_and_retires_capture(self):
        selected = self.root / "tools/checker.py"
        expected = hashlib.sha256(selected.read_bytes()).hexdigest()
        result = self.launch("build", additions={"CHECKER_FIXTURE_MUTATE": str(selected)})
        self.assertEqual(0, result.returncode, msg=result.stderr)
        evidence = json.loads((self.root / "state/build.json").read_text())
        self.assertEqual(expected, evidence["manifest"]["tools/checker.py"])
        self.assertNotEqual(Path(evidence["context"]), self.root)
        self.assertFalse(Path(evidence["context"]).exists())
        self.assertFalse(Path(evidence["dependencies"]).exists())
        self.assertEqual("sha256:" + "d" * 64 + "\n", (self.root / ".work/checker/image-id").read_text())
        calls = self.calls()
        self.assertEqual(["run", "run", "build", "image"], [call["arguments"][0] for call in calls])
        self.assertTrue(all(not call["ambient_engine"] for call in calls))
        self.assertTrue(all(call["buildkit"] == "1" for call in calls))
        build = next(call["arguments"] for call in calls if call["arguments"][0] == "build")
        self.assertEqual("default", build[build.index("--builder") + 1])

    def test_build_failure_and_wrong_observed_id_preserve_predecessor_and_retire_capture(self):
        record = self.root / ".work/checker/image-id"
        record.write_text("sha256:" + "c" * 64 + "\n")
        for additions in ({"CHECKER_FIXTURE_BUILD_FAIL": "1"}, {"CHECKER_FIXTURE_WRONG_ID": "1"}):
            with self.subTest(additions=additions):
                result = self.launch("build", additions=additions)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual("sha256:" + "c" * 64 + "\n", record.read_text())
                evidence = json.loads((self.root / "state/build.json").read_text())
                self.assertFalse(Path(evidence["context"]).exists())
                self.assertFalse(Path(evidence["dependencies"]).exists())

    def test_failed_capture_cleanup_preserves_build_status_and_reports_retained_capture(self):
        (self.root / "bin/rm").symlink_to(ROOT / "tests/test_checker_engine.py")
        record = self.root / ".work/checker/image-id"
        record.write_text("sha256:" + "c" * 64 + "\n")
        result = self.launch("build", additions={"CHECKER_FIXTURE_BUILD_FAIL": "1", "CHECKER_FIXTURE_CAPTURE_CLEANUP_FAIL": "1"})
        self.assertEqual(7, result.returncode, msg=result.stderr)
        self.assertEqual("sha256:" + "c" * 64 + "\n", record.read_text())
        evidence = json.loads((self.root / "state/build.json").read_text())
        capture = Path(evidence["context"]).parent
        self.assertTrue(Path(evidence["context"]).is_dir())
        self.assertTrue(Path(evidence["dependencies"]).is_dir())
        self.assertIn("build capture", result.stderr)
        self.assertIn(str(capture), result.stderr)
        self.assertIn("retained", result.stderr)
        self.assertEqual(
            [["-rf", "--", str(capture)]],
            [json.loads(line) for line in (self.root / "state/cleanup.jsonl").read_text().splitlines()],
        )

    def test_successful_build_with_failed_capture_cleanup_fails_and_keeps_published_record(self):
        (self.root / "bin/rm").symlink_to(ROOT / "tests/test_checker_engine.py")
        result = self.launch("build", additions={"CHECKER_FIXTURE_CAPTURE_CLEANUP_FAIL": "1"})
        self.assertEqual(13, result.returncode, msg=result.stderr)
        self.assertEqual("sha256:" + "d" * 64 + "\n", (self.root / ".work/checker/image-id").read_text())
        evidence = json.loads((self.root / "state/build.json").read_text())
        capture = Path(evidence["context"]).parent
        self.assertTrue(capture.is_dir())
        self.assertIn("build capture", result.stderr)
        self.assertIn(str(capture), result.stderr)
        self.assertEqual(
            [["-rf", "--", str(capture)]],
            [json.loads(line) for line in (self.root / "state/cleanup.jsonl").read_text().splitlines()],
        )

    def test_failed_record_rename_retires_only_owned_temporary_and_preserves_predecessor(self):
        for command in ("mv", "rm"):
            (self.root / "bin" / command).symlink_to(ROOT / "tests/test_checker_engine.py")
        record = self.root / ".work/checker/image-id"
        predecessor = "sha256:" + "c" * 64 + "\n"
        record.write_text(predecessor)
        unrelated = self.root / ".work/checker/.image-id.predecessor"
        unrelated.write_text("retained unrelated file\n")
        result = self.launch("build", additions={"CHECKER_FIXTURE_RECORD_RENAME_FAIL": "1"})
        self.assertEqual(9, result.returncode, msg=result.stderr)
        self.assertEqual(predecessor, record.read_text())
        self.assertEqual("retained unrelated file\n", unrelated.read_text())
        rename = json.loads((self.root / "state/record-rename.json").read_text())
        temporary = Path(rename[-2])
        self.assertFalse(temporary.exists())
        evidence = json.loads((self.root / "state/build.json").read_text())
        capture = Path(evidence["context"]).parent
        self.assertFalse(capture.exists())
        self.assertEqual(
            [["-f", "--", str(temporary)], ["-rf", "--", str(capture)]],
            [json.loads(line) for line in (self.root / "state/cleanup.jsonl").read_text().splitlines()],
        )

    def test_failed_record_cleanup_reports_retained_temporary_and_preserves_rename_status(self):
        for command in ("mv", "rm"):
            (self.root / "bin" / command).symlink_to(ROOT / "tests/test_checker_engine.py")
        record = self.root / ".work/checker/image-id"
        predecessor = "sha256:" + "c" * 64 + "\n"
        record.write_text(predecessor)
        result = self.launch(
            "build", additions={"CHECKER_FIXTURE_RECORD_RENAME_FAIL": "1", "CHECKER_FIXTURE_RECORD_CLEANUP_FAIL": "1"}
        )
        self.assertEqual(9, result.returncode, msg=result.stderr)
        self.assertEqual(predecessor, record.read_text())
        rename = json.loads((self.root / "state/record-rename.json").read_text())
        temporary = Path(rename[-2])
        self.assertEqual("sha256:" + "d" * 64 + "\n", temporary.read_text())
        self.assertIn("unpublished checker image record", result.stderr)
        self.assertIn("retained", result.stderr)
        self.assertIn(str(temporary), result.stderr)
        evidence = json.loads((self.root / "state/build.json").read_text())
        capture = Path(evidence["context"]).parent
        self.assertFalse(capture.exists())
        self.assertEqual(
            [["-f", "--", str(temporary)], ["-rf", "--", str(capture)]],
            [json.loads(line) for line in (self.root / "state/cleanup.jsonl").read_text().splitlines()],
        )

    def test_check_preview_is_offline_readonly_and_download_only_writes_owned_stage(self):
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        check = self.launch("check", preview=True)
        self.assertEqual(0, check.returncode, msg=check.stderr)
        arguments = shlex.split(check.stdout)
        self.assertEqual("none", arguments[arguments.index("--network") + 1])
        mounts = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--mount"]
        self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
        download = self.launch("download", preview=True)
        self.assertEqual(0, download.returncode, msg=download.stderr)
        worker = shlex.split(download.stdout.splitlines()[-2])
        mounts = [worker[index + 1] for index, value in enumerate(worker) if value == "--mount"]
        writable = [mount for mount in mounts if not mount.endswith(",readonly")]
        self.assertEqual([f"type=bind,src={self.root}/.work/checker,dst=/output"], writable)
        self.assertNotIn("/var/run/docker.sock", " ".join(mounts))
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")))
