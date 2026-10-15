"""Exercise checker capture, publication and grants with a contained finite engine."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from unittest.mock import patch

from tests.test_checker import CheckerDependencyFixture

# Both maintained test lanes set /workspace as the working directory; fixture
# source paths use that explicit runner contract rather than this file's layout.
ROOT = Path.cwd()


class CheckerLauncherTests(CheckerDependencyFixture):
    def setUp(self):
        super().setUp()
        # Like LauncherFixture's engine log, only constructor-owned emptiness
        # certifies that no calls have been observed; writers cannot recreate it.
        for directory in ("tools", "requirements", "containers/checker", "state", "state/calls", "bin"):
            (self.root / directory).mkdir(parents=True)
        for name in (
            "checker.py",
            "checker.sh",
            "artifacts.py",
            "container_guard.py",
            "docker.sh",
            "docker_client.sh",
            "inference_native.sh",
        ):
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
        self.retained = set()
        self.addCleanup(self._remove_retained)
        # Normal response inputs belong to this fixture; per-call additions
        # still supply raw malformed or mismatched responses deliberately.
        self.environment = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "PATH": str(self.root / "bin") + ":" + str(Path(sys.executable).parent) + ":" + os.defpath,
            "CHECKER_FIXTURE_STATE": str(self.root / "state"),
            "CHECKER_FIXTURE_BUILD_OUTPUT": "sha256:" + "d" * 64 + "\n",
            "CHECKER_FIXTURE_INSPECTED_ID": "sha256:" + "d" * 64,
        }

    # silent-defaults: allow-contract -- Omitted preview runs the finite checker client; explicit preview must print the same worker without another engine call; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_private_client_scrubs_poison_and_check_matches_printed_worker
    def launch(self, action, *, preview=False, additions=None):
        environment = {
            **self.environment,
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
        result = subprocess.run([*command, action], env=environment, text=True, capture_output=True, timeout=10, check=False)
        self._remember(result.stderr)
        return result

    def calls(self):
        # Iterating first preserves missing-directory failure instead of glob's
        # empty result. Published JSON remains the existing observation format.
        return [
            json.loads(path.read_text()) for path in sorted((self.root / "state/calls").iterdir()) if path.name.endswith(".json")
        ]

    def _remember(self, stderr):
        self.retained.update(
            Path(path)
            for path in re.findall(r"/tmp/nmrnet-(?:docker-client|inference-native|checker-admission)\.[A-Za-z0-9]+", stderr)
        )

    def _remove_retained(self):
        for path in self.retained:
            subprocess.run(["/bin/rm", "-rf", "--", str(path)], check=True)

    def _observe_wrappers(self, *names):
        for name in names:
            target = self.root / "bin" / name
            if not target.exists():
                target.symlink_to(ROOT / "tests/test_checker_engine.py")

    # silent-defaults: allow-contract -- Capture both streams unless a blocked-pipe scenario supplies its own descriptor; the removal-signal case checks exact stdout and quiet stderr; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_actual_removal_signal_finishes_quiet_cleanup_without_new_report
    def _start(self, action, additions, *, stdout=subprocess.PIPE, stderr=subprocess.PIPE):
        environment = self.environment | additions
        process = subprocess.Popen(
            ["/bin/bash", str(self.root / "tools/checker.sh"), action],
            env=environment,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self._retire_failed_probe, process)
        return process

    def _wait_path(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), msg="actual checker fixture phase did not publish its observation")

    def _retire_failed_probe(self, process):
        if process.poll() is None:
            # Only the held, unreaped caller identity and exact live native
            # anchors in its session admit rescue; old fixture PIDs do not.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            deadline = time.monotonic() + 3
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                self.assertLess(time.monotonic(), deadline, msg="failed-probe caller death was not observed")
                time.sleep(0.01)
            calls_path = self.root / "state/calls"
            calls = self.calls() if calls_path.exists() else []
            groups = {call["process_group"] for call in calls}
            for name in ("report.json", "cleanup-report.json"):
                path = self.root / "state" / name
                if path.exists():
                    groups.add(json.loads(path.read_text())["anchor"])
            admitted = set()
            paths = set()
            for group in groups:
                with suppress(FileNotFoundError, ProcessLookupError):
                    arguments = Path("/proc", str(group), "cmdline").read_bytes().split(b"\0")
                    if (
                        os.getsid(group) == process.pid
                        and len(arguments) >= 6
                        and arguments[:2] == [b"bash", b"-c"]
                        and b"receipt=$1 parent=$2 budget=$3 timed_program=$4 deadline_signal=$5 grace=$6; shift 6"
                        in arguments[2]
                        and arguments[5] == str(process.pid).encode()
                    ):
                        os.killpg(group, signal.SIGKILL)
                        admitted.add(group)
                        paths.add(Path(arguments[4].decode()).parent)
                if group in admitted:
                    for call in calls:
                        if call["process_group"] == group:
                            paths.add(Path(call["client_config"]).parent)
                            if call["receipt_directory"] is not None:
                                paths.add(Path(call["receipt_directory"]))
            for group in admitted:
                while True:
                    try:
                        os.killpg(group, 0)
                    except ProcessLookupError:
                        break
                    self.assertLess(time.monotonic(), deadline, msg="failed-probe native group retirement was not observed")
                    time.sleep(0.01)
            self.retained.update(paths)
            process.communicate(timeout=3)

    def _full_pipe(self):
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        os.set_blocking(writer, False)
        try:
            while True:
                os.write(writer, b"x" * 4096)
        except BlockingIOError:
            pass
        os.set_blocking(writer, True)
        return reader, writer

    def _wait_blocked_report(self, process, name, *, primary=None):
        path = self.root / "state" / name
        self._wait_path(path)
        observed = json.loads(path.read_text())
        if name == "report.json":
            program = '\n    printf "Checker image recorded: %s\\n" "$1"\n'
            data = [b"bash", ("sha256:" + "d" * 64).encode()]
        else:
            source = (self.root / "tools/checker.sh").read_text()
            start = "if inference_native 'checker cleanup reporting' untimed stdio bash -eu -c '"
            end = '\' bash "$inference_diagnostic"'
            self.assertEqual(1, source.count(start))
            self.assertEqual(1, source.count(end))
            program = source.split(start, 1)[1].split(end, 1)[0]
            receipt = self.calls()[-1]["receipt_directory"]
            data = [
                b"bash",
                f"Cannot record checker image: local image inspection failed (status {primary}).\n".encode(),
                b"",
                b"0",
                b"",
                b"0",
                b"",
                b"0",
                receipt.encode(),
                b"0",
            ]
        expected = [b"bash", b"-eu", b"-c", program.encode(), *data]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                command = Path("/proc", str(observed["pid"]), "cmdline").read_bytes().split(b"\0")[:-1]
                waiting = Path("/proc", str(observed["pid"]), "wchan").read_text().strip()
                anchor = Path("/proc", str(observed["anchor"]), "cmdline").read_bytes().split(b"\0")
                if (
                    command == expected
                    and waiting in {"pipe_write", "anon_pipe_write"}
                    and os.getpgid(observed["pid"]) == observed["anchor"]
                    and os.getsid(observed["anchor"]) == process.pid
                    and len(anchor) >= 6
                    and b"receipt=$1 parent=$2 budget=$3 timed_program=$4 deadline_signal=$5 grace=$6; shift 6" in anchor[2]
                    and anchor[5] == str(process.pid).encode()
                ):
                    return observed
            except (FileNotFoundError, ProcessLookupError):
                pass
            time.sleep(0.01)
        raise AssertionError("actual checker reporter was not observed waiting on its full output pipe under the held owner")

    def test_constructor_responses_override_ambient_faults(self):
        with patch.dict(
            os.environ,
            {
                "CHECKER_FIXTURE_MALFORMED_ID": "1",
                "CHECKER_FIXTURE_WRONG_ID": "1",
                "CHECKER_FIXTURE_BUILD_OUTPUT": "ambient malformed\n",
                "CHECKER_FIXTURE_INSPECTED_ID": "sha256:" + "e" * 64,
            },
        ):
            fixture = CheckerLauncherTests()
            # A manually set-up case reports cleanup failure through its bool;
            # the parent's _callCleanup ignores returns, so assert at teardown.
            # CPython 3.12.12 doCleanups/_callCleanup:
            # https://raw.githubusercontent.com/python/cpython/v3.12.12/Lib/unittest/case.py
            self.addCleanup(lambda: self.assertTrue(fixture.doCleanups(), "Nested checker fixture cleanup failed."))
            fixture.setUp()
            result = fixture.launch("build")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("Checker image recorded: sha256:" + "d" * 64 + "\n", result.stdout)
        self.assertEqual("sha256:" + "d" * 64 + "\n", (fixture.root / ".work/checker/image-id").read_text())

    def test_missing_selected_response_fails_without_inventing_response_effects(self):
        record = self.root / ".work/checker/image-id"
        predecessor = b"sha256:" + b"c" * 64 + b"\n"
        for field, prefix, cause_file, operations in (
            ("CHECKER_FIXTURE_BUILD_OUTPUT", "nmrnet-inference-native.", "command.log", ["run", "run", "build"]),
            ("CHECKER_FIXTURE_INSPECTED_ID", "nmrnet-checker-admission.", "stderr", ["run", "run", "build", "image"]),
        ):
            with self.subTest(field=field):
                record.write_bytes(predecessor)
                before_calls, before_retained = len(self.calls()), set(self.retained)
                value = self.environment.pop(field)
                try:
                    result = self.launch("build")
                    self.assertEqual(1, result.returncode, msg=result.stderr)
                    self.assertEqual("", result.stdout)
                    self.assertEqual(predecessor, record.read_bytes())
                    self.assertEqual(operations, [call["arguments"][0] for call in self.calls()[before_calls:]])
                    evidence = [path for path in self.retained - before_retained if path.name.startswith(prefix)]
                    self.assertEqual(1, len(evidence), msg=result.stderr)
                    self.assertIn(f"KeyError: '{field}'".encode(), (evidence[0] / cause_file).read_bytes())
                    if field == "CHECKER_FIXTURE_BUILD_OUTPUT":
                        self.assertFalse((self.root / "state/build.json").exists())
                        self.assertFalse((self.root / "state/label").exists())
                        failed = self.launch("build", additions={"CHECKER_FIXTURE_BUILD_FAIL": "1"})
                        self.assertEqual(7, failed.returncode, msg=failed.stderr)
                        self.assertEqual("", failed.stdout)
                        self.assertEqual(predecessor, record.read_bytes())
                    else:
                        receipt = Path(self.calls()[-1]["receipt_directory"])
                        self.assertEqual(evidence[0], receipt)
                        self.assertEqual(b"", (receipt / "stdout").read_bytes())
                finally:
                    self.environment[field] = value

    def test_owned_empty_call_ledger_and_lost_ledger_have_distinct_authority(self):
        ledger = self.root / "state/calls"
        self.assertTrue(ledger.is_dir())
        self.assertEqual([], self.calls())
        ordinary = self.launch("check")
        self.assertEqual(0, ordinary.returncode, msg=ordinary.stderr)
        calls = self.calls()
        self.assertEqual(1, len(calls))
        self.assertEqual("run", calls[0]["arguments"][0])
        (ledger / "000000.json").unlink()
        ledger.rmdir()
        with self.assertRaises(FileNotFoundError):
            self.calls()
        record = self.root / ".work/checker/image-id"
        predecessor = b"sha256:" + b"c" * 64 + b"\n"
        record.write_bytes(predecessor)
        rejected = self.launch("build")
        self.assertEqual(1, rejected.returncode, msg=rejected.stderr)
        self.assertEqual("", rejected.stdout)
        cause = f"FileNotFoundError: [Errno 2] No such file or directory: '{ledger}'"
        self.assertIn(cause, rejected.stderr)
        self.assertFalse(ledger.exists())
        self.assertEqual([], list((self.root / "state").iterdir()))
        self.assertEqual(predecessor, record.read_bytes())
        self.assertEqual({"dependencies", "image-id"}, {path.name for path in record.parent.iterdir()})

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
        for additions in ({"CHECKER_FIXTURE_BUILD_FAIL": "1"}, {"CHECKER_FIXTURE_INSPECTED_ID": "sha256:" + "e" * 64}):
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
        self.assertIn("attempted path", result.stderr)
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
        self.assertIn("attempted path", result.stderr)
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

    def test_private_client_scrubs_poison_and_check_matches_printed_worker(self):
        poison = self.root / "poison"
        poison.mkdir()
        (poison / "config.json").write_text('{"proxies":{"default":{"httpsProxy":"poison"}}}')
        forbidden = (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "DOCKER_AUTH_CONFIG",
            "DOCKER_CONFIG",
            "DOCKER_CUSTOM_HEADERS",
            "DOCKER_API_VERSION",
            "DOCKER_DEFAULT_PLATFORM",
            "BUILDX_BUILDER",
            "BUILDX_CONFIG",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        )
        result = self.launch("check", additions={**dict.fromkeys(forbidden, "poison"), "DOCKER_CONFIG": str(poison)})
        self.assertEqual(0, result.returncode, msg=result.stderr)
        call = self.calls()[-1]
        self.assertEqual([], call["config_entries"])
        self.assertEqual(0o700, call["config_mode"])
        self.assertEqual({"DOCKER_BUILDKIT": "1"}, call["environment"])
        self.assertTrue((poison / "config.json").is_file())
        self.assertFalse(Path(call["client_config"]).parent.exists())
        printed = self.launch("check", preview=True)
        self.assertEqual(0, printed.returncode, msg=printed.stderr)
        arguments = shlex.split(printed.stdout)
        self.assertEqual(call["arguments"], arguments[arguments.index("run") :])
        self.assertEqual(1, len(self.calls()))

    def test_check_preserves_native_status_and_streams(self):
        expected = hashlib.sha256((self.root / "requirements/checker-dependencies.json").read_bytes()).hexdigest() + "\n"
        for status in (0, 37):
            with self.subTest(status=status):
                result = self.launch("check", additions={"CHECKER_FIXTURE_RUN_STATUS": str(status)})
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertEqual(expected, result.stdout)
                self.assertEqual("checker run diagnostic\n", result.stderr)
                self.assertFalse(Path(self.calls()[-1]["client_config"]).parent.exists())

    def test_raw_combined_receipt_and_build_identity_gate_publication(self):
        good = self.launch("build")
        self.assertEqual(0, good.returncode, msg=good.stderr)
        self.assertEqual("Checker image recorded: sha256:" + "d" * 64 + "\n", good.stdout)
        self.assertFalse(Path(self.calls()[-1]["receipt_directory"]).exists())
        record = self.root / ".work/checker/image-id"
        predecessor = "sha256:" + "c" * 64 + "\n"
        for fault in ("malformed-build", "missing-lf", "extra-lf", "wrong-label", "cap-stdout", "cap-stderr", "link", "fifo"):
            with self.subTest(fault=fault):
                record.write_text(predecessor)
                before = len(self.calls())
                # silent-defaults: allow-contract -- Malformed build output and malformed inspection receipts are explicit faults at different admission phases; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_raw_combined_receipt_and_build_identity_gate_publication
                additions = (
                    {"CHECKER_FIXTURE_BUILD_OUTPUT": "malformed\n"}
                    if fault == "malformed-build"
                    else {"CHECKER_FIXTURE_RECEIPT": fault}
                )
                result = self.launch("build", additions=additions)
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertEqual(predecessor, record.read_text())
                calls = self.calls()[before:]
                self.assertEqual(
                    ["run", "run", "build"] + ([] if fault == "malformed-build" else ["image"]),
                    [call["arguments"][0] for call in calls],
                )
                evidence = json.loads((self.root / "state/build.json").read_text())
                self.assertFalse(Path(evidence["context"]).parent.exists())
                self.assertFalse(Path(calls[-1]["client_config"]).parent.exists())
                if fault != "malformed-build":
                    receipt = Path(calls[-1]["receipt_directory"])
                    self.retained.add(receipt)
                    self.assertTrue(receipt.is_dir())
                    self.assertIn(str(receipt), result.stderr)
                    if fault.startswith("cap-"):
                        # silent-defaults: allow-contract -- Each cap scenario names the private stream whose observed receipt must reach the byte ceiling; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_raw_combined_receipt_and_build_identity_gate_publication
                        stream = "stdout" if fault == "cap-stdout" else "stderr"
                        self.assertEqual(4096, (receipt / stream).stat().st_size)
                        self.assertIn("4096-byte limit", result.stderr)
                    elif fault in {"link", "fifo"}:
                        self.assertIn("regular files without symlinks", result.stderr)

    def test_ordinary_inspection_signal_statuses_keep_private_daemon_cause(self):
        self._observe_wrappers("bash")
        for status in (129, 130, 143):
            with self.subTest(status=status):
                reporter = self.root / "state/cleanup-report.json"
                reporter.unlink(missing_ok=True)
                additions = {"CHECKER_FIXTURE_INSPECT_STATUS": str(status)}
                if status == 130:
                    reader, writer = self._full_pipe()
                    process = self._start("build", additions, stderr=writer)
                    os.close(writer)
                    self._wait_blocked_report(process, "cleanup-report.json", primary=130)
                    self.assertIsNone(process.poll())
                    # Ordinary130 still owns a useful report; release its full
                    # pipe only after proving the actual publisher is waiting.
                    os.read(reader, 65536)
                    stdout, _ = process.communicate(timeout=5)
                    stderr = os.read(reader, 65536).decode()
                    result = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
                    self._remember(stderr)
                else:
                    result = self.launch("build", additions=additions)
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertIn("local image inspection failed", result.stderr)
                receipt = Path(self.calls()[-1]["receipt_directory"])
                self.retained.add(receipt)
                self.assertIn(str(receipt), result.stderr)
                self.assertEqual(b"checker daemon cause\n", (receipt / "stderr").read_bytes())
                self.assertFalse(Path(self.calls()[-1]["client_config"]).parent.exists())
                self.assertFalse(Path(json.loads((self.root / "state/build.json").read_text())["context"]).parent.exists())
                self.assertFalse((self.root / ".work/checker/image-id").exists())

    def test_client_and_receipt_removal_failures_report_attempt_truth(self):
        self._observe_wrappers("rm")
        for kind, primary, deletion in (
            ("CLIENT", 0, False),
            ("CLIENT", 37, False),
            ("CLIENT", 0, True),
            ("ADMISSION", 0, False),
            ("ADMISSION", 0, True),
        ):
            with self.subTest(kind=kind, primary=primary, deletion=deletion):
                # silent-defaults: allow-contract -- Closed cleanup stimuli select delete-then-fail or failure without removal; assertions inspect the actual directory effect; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_client_and_receipt_removal_failures_report_attempt_truth
                additions = {f"CHECKER_FIXTURE_{kind}_CLEANUP_FAIL": "delete" if deletion else "retain"}
                if kind == "CLIENT":
                    additions["CHECKER_FIXTURE_RUN_STATUS"] = str(primary)
                # silent-defaults: allow-contract -- Client cleanup is exercised through check and admission-receipt cleanup through build; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_client_and_receipt_removal_failures_report_attempt_truth
                result = self.launch("check" if kind == "CLIENT" else "build", additions=additions)
                # silent-defaults: allow-contract -- A nonzero primary wins; otherwise the client and admission cleanup faults expect their distinct statuses 41 and 42; proof=tests/test_checker_launcher.py::CheckerLauncherTests.test_client_and_receipt_removal_failures_report_attempt_truth
                self.assertEqual(primary or (41 if kind == "CLIENT" else 42), result.returncode, msg=result.stderr)
                self.assertIn("attempted path", result.stderr)
                call = self.calls()[-1]
                target = Path(call["client_config"]).parent if kind == "CLIENT" else Path(call["receipt_directory"])
                self.retained.add(target)
                self.assertEqual(not deletion, target.exists())
                self.assertIn(str(target), result.stderr)
                if kind == "ADMISSION":
                    self.assertEqual("sha256:" + "d" * 64 + "\n", (self.root / ".work/checker/image-id").read_text())

    def test_unknown_inspection_startup_retains_all_paths_without_next_owner(self):
        self._observe_wrappers("bash", "mv")
        result = self.launch("build", additions={"CHECKER_FIXTURE_READY_FAIL": "inspect"})
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertEqual("ready-failure\n", (self.root / "state/ready-fault").read_text())
        self.assertEqual("4", (self.root / "state/ready-count").read_text())
        self.assertEqual(["run", "run", "build"], [call["arguments"][0] for call in self.calls()])
        self.assertIn("Cannot confirm checker native client retirement", result.stderr)
        self.assertTrue(Path(self.calls()[-1]["client_config"]).parent.is_dir())
        self.assertTrue(Path(json.loads((self.root / "state/build.json").read_text())["context"]).parent.is_dir())
        receipts = [path for path in self.retained if path.name.startswith("nmrnet-checker-admission.")]
        self.assertEqual(1, len(receipts))
        self.assertTrue(receipts[0].is_dir())
        self.assertFalse((self.root / "state/cleanup-report.json").exists())
        self.assertFalse((self.root / "state/report.json").exists())
        self.assertFalse((self.root / ".work/checker/image-id").exists())

    def test_actual_inspection_signals_exit_before_draining_full_stderr(self):
        self._observe_wrappers("bash")
        record = self.root / ".work/checker/image-id"
        predecessor = "sha256:" + "c" * 64 + "\n"
        record.write_text(predecessor)
        for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupt=interrupt):
                ready = self.root / "state/client-ready"
                ready.unlink(missing_ok=True)
                reader, writer = self._full_pipe()
                process = self._start("build", {"CHECKER_FIXTURE_HOLD": "inspect"}, stderr=writer)
                os.close(writer)
                self._wait_path(ready)
                call = self.calls()[-1]
                self.assertEqual(["image", "inspect"], call["arguments"][:2])
                self.assertEqual(str(call["pid"]), ready.read_text())
                self.assertEqual(call["process_group"], os.getpgid(call["pid"]))
                receipt = Path(call["receipt_directory"])
                self.retained.add(receipt)
                process.send_signal(interrupt)
                self.assertEqual(status, process.wait(timeout=5))
                # The sole signal retires the actual native client and completes
                # permitted cleanup before ANY stderr bytes are drained.
                self.assertFalse(Path("/proc", str(call["pid"])).exists())
                self.assertFalse(Path(call["client_config"]).parent.exists())
                self.assertFalse(Path(json.loads((self.root / "state/build.json").read_text())["context"]).parent.exists())
                self.assertTrue(receipt.is_dir())
                self.assertEqual(predecessor, record.read_text())
                self.assertFalse((self.root / "state/cleanup-report.json").exists())
                process.communicate(timeout=3)
                self.assertEqual({ord("x")}, set(os.read(reader, 65536)))

    def test_actual_check_signals_retire_client_before_configuration(self):
        for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupt=interrupt):
                ready = self.root / "state/client-ready"
                ready.unlink(missing_ok=True)
                process = self._start("check", {"CHECKER_FIXTURE_HOLD": "run"})
                self._wait_path(ready)
                call = self.calls()[-1]
                process.send_signal(interrupt)
                _, stderr = process.communicate(timeout=5)
                self._remember(stderr)
                self.assertEqual(status, process.returncode, msg=stderr)
                self.assertFalse(Path("/proc", str(call["pid"])).exists())
                self.assertFalse(Path(call["client_config"]).parent.exists())
                self.assertEqual("", stderr)

    def test_actual_success_report_signals_finish_before_stdout_drain(self):
        self._observe_wrappers("bash")
        for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupt=interrupt):
                report = self.root / "state/report.json"
                report.unlink(missing_ok=True)
                reader, writer = self._full_pipe()
                process = self._start("build", {}, stdout=writer)
                os.close(writer)
                observed = self._wait_blocked_report(process, "report.json")
                call = self.calls()[-1]
                record = self.root / ".work/checker/image-id"
                record_inode = record.stat().st_ino
                self.assertEqual("sha256:" + "d" * 64 + "\n", record.read_text())
                process.send_signal(interrupt)
                self.assertEqual(status, process.wait(timeout=5))
                self.assertEqual("sha256:" + "d" * 64 + "\n", record.read_text())
                self.assertEqual(record_inode, record.stat().st_ino)
                self.assertFalse(Path("/proc", str(observed["pid"])).exists())
                self.assertFalse(Path(call["client_config"]).parent.exists())
                receipt = Path(call["receipt_directory"])
                self.retained.add(receipt)
                self.assertTrue(receipt.is_dir())
                self.assertEqual(137, (receipt / "stdout").stat().st_size)
                self.assertFalse(Path(json.loads((self.root / "state/build.json").read_text())["context"]).parent.exists())
                _, stderr = process.communicate(timeout=3)
                self.assertEqual("", stderr)
                self.assertEqual({ord("x")}, set(os.read(reader, 65536)))

    def test_actual_removal_signal_finishes_quiet_cleanup_without_new_report(self):
        self._observe_wrappers("bash", "rm")
        process = self._start("build", {"CHECKER_FIXTURE_HOLD_REMOVAL": "1"})
        self._wait_path(self.root / "state/removal-ready")
        call = self.calls()[-1]
        process.send_signal(signal.SIGTERM)
        (self.root / "state/removal-release").write_text("release\n")
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(143, process.returncode, msg=stderr)
        self.assertEqual("Checker image recorded: sha256:" + "d" * 64 + "\n", stdout)
        self.assertEqual("", stderr)
        self.assertFalse(Path(call["client_config"]).parent.exists())
        self.assertFalse(Path(call["receipt_directory"]).exists())
        self.assertFalse(Path(json.loads((self.root / "state/build.json").read_text())["context"]).parent.exists())
        self.assertFalse((self.root / "state/cleanup-report.json").exists())
        self.assertEqual("sha256:" + "d" * 64 + "\n", (self.root / ".work/checker/image-id").read_text())

    def test_cleanup_report_actual_signal_overrides_primary_but_ordinary130_does_not(self):
        self._observe_wrappers("bash")
        ordinary = self.launch(
            "build", additions={"CHECKER_FIXTURE_INSPECT_STATUS": "37", "CHECKER_FIXTURE_REPORT_STATUS": "130"}
        )
        self.assertEqual(37, ordinary.returncode, msg=ordinary.stderr)
        receipt = Path(self.calls()[-1]["receipt_directory"])
        self.retained.add(receipt)
        self.assertEqual(b"checker daemon cause\n", (receipt / "stderr").read_bytes())
        (self.root / "state/cleanup-report.json").unlink()
        reader, writer = self._full_pipe()
        process = self._start("build", {"CHECKER_FIXTURE_INSPECT_STATUS": "37"}, stderr=writer)
        os.close(writer)
        observed = self._wait_blocked_report(process, "cleanup-report.json", primary=37)
        call = self.calls()[-1]
        receipt = Path(call["receipt_directory"])
        self.retained.add(receipt)
        self.assertFalse(Path(call["client_config"]).parent.exists())
        process.send_signal(signal.SIGINT)
        self.assertEqual(130, process.wait(timeout=5))
        self.assertFalse(Path("/proc", str(observed["pid"])).exists())
        self.assertEqual(b"checker daemon cause\n", (receipt / "stderr").read_bytes())
        self.assertFalse((self.root / ".work/checker/image-id").exists())
        process.communicate(timeout=3)
        self.assertEqual({ord("x")}, set(os.read(reader, 65536)))
