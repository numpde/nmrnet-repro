#!/usr/bin/env python3
"""Observe package build/apply client ownership without builds or acquisition.

The stage controller is a finite double: proxy/container lifetime remains the
separate docker.sh contract. Native clients and Review publishers are real child
processes inside the contained lane, with stdout backpressure and fault probes.
"""

from tools.container_guard import require_container

require_container()

import fcntl
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path

from repository_checks.fixtures.containers.fixture import LauncherFixture, record_engine_call


class PackagesClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = LauncherFixture(self.root)
        self.addCleanup(self.fixture.dispose)
        self.retained = set()
        self.addCleanup(self._remove_retained)
        self.environment = self.fixture.environment | {"NMRNET_PACKAGE_FIXTURE_ROOT": str(self.root)}
        for name in ("docker", "rm", "mv"):
            target = self.root / "bin" / name
            target.unlink(missing_ok=True)
            target.symlink_to(Path(__file__).resolve())
        original = self.root / "tools/docker.original.sh"
        (self.root / "tools/docker.sh").rename(original)
        # These faults belong to package-owned native clients.
        # AcquisitionClientTests in test_acquisition_client_config.py and
        # EngineEnvironmentTests in test_operator_inputs_port.py exercise the
        # actual controller's preflight. Here admit only the exact pinned pair.
        (self.root / "tools/docker.sh").write_text(
            "#!/bin/bash\n"
            'if [[ "$1" == require-images ]]; then\n'
            '    if [[ $# != 3 || "$2" != '
            "'docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'"
            ' || "$3" != '
            "'ghcr.io/astral-sh/uv@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d' ]]; then\n"
            "        printf '%s\\n' 'Unsupported package fixture preload request: expected the pinned Python and uv pair.' >&2\n"
            "        exit 97\n"
            "    fi\n"
            "    exit 0\n"
            "fi\n"
            'if [[ "$1" == run ]]; then\n'
            f"    exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} controller\n"
            "fi\n"
            f'exec bash {shlex.quote(str(original))} "$@"\n'
        )
        self.stage = self.root / ".work/packages-stage/lock.fixture"
        self.stage.mkdir(parents=True)
        self.identity = b"sha256:" + b"b" * 64 + b"\n"
        (self.stage / "image-id").write_bytes(self.identity)
        (self.root / "build-output").write_bytes(self.identity)
        (self.root / "native-status").write_text("0")
        (self.root / "observed-output").write_bytes(self.identity)
        (self.root / "inspect-status").write_text("0")

    def _remove_retained(self):
        for path in self.retained:
            subprocess.run(["/bin/rm", "-rf", "--", str(path)], check=True)

    def _remember(self, stderr):
        for path in re.findall(r"/tmp/nmrnet-(?:docker-client|inference-native)\.[A-Za-z0-9]+", stderr):
            self.retained.add(Path(path))

    def caller(self, *arguments, environment):
        result = subprocess.run(
            ["bash", str(self.root / "tools/packages.sh"), *arguments],
            env=environment,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        self._remember(result.stderr)
        return result

    def apply_environment(self):
        return self.environment | {"NMRNET_STAGE_INPUT": "lock.fixture"}

    def owned_calls(self):
        return [call for call in self.fixture.calls() if call["arguments"][0] in {"build", "run"}]

    def test_changed_preload_pin_is_rejected_before_package_effects(self):
        source = self.root / "tools/packages.sh"
        before = source.read_text()
        pin = "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
        self.assertEqual(1, before.count(pin))
        source.write_text(before.replace(pin, "docker.io/library/python:3.12.12-slim-bookworm@sha256:" + "f" * 64))
        allocation = self.root / "unexpected-allocation"
        stages = sorted(path.name for path in self.stage.parent.iterdir())
        result = self.caller(
            "stage",
            environment=self.environment
            | {
                "NMRNET_WLAN_INTERFACE_INPUT": "lo",
                "NMRNET_PACKAGE_ALLOCATION_OBSERVATION": str(allocation),
                "BASH_FUNC_mktemp%%": '() { printf "unexpected allocation\\n" > "$NMRNET_PACKAGE_ALLOCATION_OBSERVATION"; return 96; }',
            },
        )
        self.assertEqual(97, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        self.assertIn("expected the pinned Python and uv pair", result.stderr)
        self.assertFalse(self.fixture.calls())
        self.assertFalse(allocation.exists())
        self.assertFalse((self.root / "controller-returned").exists())
        self.assertEqual(stages, sorted(path.name for path in self.stage.parent.iterdir()))

    def test_print_and_input_rejection_do_not_prepare_or_dispatch_client(self):
        ambient = self.root / "ambient-owned-name"
        ambient.mkdir()
        environment = self.apply_environment() | {"docker_client_directory": str(ambient)}
        for arguments in ((), ("--print",), ("unknown",), ("pull",), ("apply", "extra"), ("--print", "apply")):
            with self.subTest(arguments=arguments):
                result = self.caller(*arguments, environment=environment)
                self.assertEqual(0 if arguments == ("--print", "apply") else 2, result.returncode, msg=result.stderr)
                self.assertTrue(ambient.is_dir())
                self.assertFalse(self.fixture.calls())
        (self.stage / "image-id").write_text("mutable:tag\n")
        result = self.caller("apply", environment=environment)
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertIn("invalid local resolver image ID", result.stderr)
        self.assertFalse(self.fixture.calls())

    def test_build_and_apply_use_empty_private_config_and_fixed_client_environment(self):
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
        environment = (
            self.apply_environment()
            | dict.fromkeys(forbidden, "poison")
            | {
                "DOCKER_CONFIG": str(poison),
                "DOCKER_BUILDKIT": "0",
                "NMRNET_WLAN_INTERFACE_INPUT": "lo",
            }
        )
        for action in ("stage", "apply"):
            result = self.caller(action, environment=environment)
            self.assertEqual(0, result.returncode, msg=result.stderr)
            if action == "stage":
                staged = [path for path in self.stage.parent.iterdir() if path != self.stage]
                self.assertEqual(1, len(staged))
                self.assertEqual(self.identity, (staged[0] / "image-id").read_bytes())
                self.assertEqual(
                    f"Review {staged[0]}, then apply with make packages/lock/apply STAGE={staged[0].name}\n", result.stdout
                )
        calls = self.owned_calls()
        self.assertEqual(["build", "run"], [call["arguments"][0] for call in calls])
        for call in calls:
            self.assertEqual([], call["config_entries"])
            self.assertEqual(0o700, call["config_mode"])
            self.assertEqual("1", call["environment"]["DOCKER_BUILDKIT"])
            self.assertTrue(set(forbidden).isdisjoint(call["environment"]))
            self.assertNotEqual(str(poison), call["config"])
            self.assertFalse(Path(call["config"]).parent.exists())
        self.assertTrue((poison / "config.json").is_file())
        self.assertEqual("default", calls[0]["arguments"][calls[0]["arguments"].index("--builder") + 1])
        apply = calls[1]["arguments"]
        for flag in ("--init", "--read-only", "--cap-drop", "--security-opt"):
            self.assertIn(flag, apply)
        self.assertEqual("none", apply[apply.index("--network") + 1])
        self.assertEqual("2g", apply[apply.index("--memory") + 1])

    def test_apply_preserves_native_output_and_failure_status(self):
        for status in (0, 37):
            with self.subTest(status=status):
                (self.root / "native-status").write_text(str(status))
                result = self.caller("apply", environment=self.apply_environment())
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertEqual("package output\n", result.stdout)
                self.assertEqual("package diagnostic\n", result.stderr)
                self.assertFalse(Path(self.owned_calls()[-1]["config"]).parent.exists())

    def test_build_failure_preserves_status_evidence_and_blocks_controller(self):
        (self.root / "native-status").write_text("37")
        result = self.caller("stage", environment=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo"})
        self.assertEqual(37, result.returncode, msg=result.stderr)
        self.assertIn("native client exited with status 37", result.stderr)
        self.assertFalse((self.root / "controller-returned").exists())
        staged = [path for path in self.stage.parent.iterdir() if path != self.stage]
        self.assertEqual(1, len(staged))
        self.assertFalse((staged[0] / "image-id").exists())
        self.assertFalse(Path(self.owned_calls()[-1]["config"]).parent.exists())

    def test_cleanup_failure_changes_only_success_and_preserves_attempt_truth(self):
        for status, delete in ((0, False), (37, False), (0, True)):
            with self.subTest(status=status, delete=delete):
                (self.root / "native-status").write_text(str(status))
                result = self.caller(
                    "apply", environment=self.apply_environment() | {"NMRNET_PACKAGE_RM_FAULT": "delete" if delete else "retain"}
                )
                self.assertEqual(status or 41, result.returncode, msg=result.stderr)
                self.assertIn("Cannot confirm package Docker client configuration removal; attempted path", result.stderr)
                self.assertNotIn("configuration retained", result.stderr)
                directory = Path(self.owned_calls()[-1]["config"]).parent
                self.assertEqual(not delete, directory.exists())
                self.retained.add(directory)

    def test_unconfirmed_startup_keeps_config_and_never_starts_another_owner(self):
        result = self.caller(
            "stage", environment=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo", "NMRNET_PACKAGE_READY_FAULT": "1"}
        )
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertEqual("ready-failure\n", (self.root / "ready-fault").read_text())
        self.assertIn("Cannot confirm package native client retirement", result.stderr)
        self.assertFalse(self.owned_calls())
        self.assertFalse((self.root / "controller-returned").exists())
        directories = [path for path in self.retained if path.name.startswith("nmrnet-docker-client.")]
        self.assertEqual(1, len(directories))
        self.assertTrue(directories[0].is_dir())

    def test_malformed_build_identity_is_rejected_before_stage_publication(self):
        for body in (self.identity[:-1], self.identity + b"extra\n", b"mutable:tag\n", self.identity[:-1] + b"\0\n"):
            with self.subTest(body=body):
                (self.root / "build-output").write_bytes(body)
                result = self.caller("stage", environment=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo"})
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertFalse((self.root / "controller-returned").exists())
                for stage in (self.root / ".work/packages-stage").iterdir():
                    if stage != self.stage:
                        self.assertFalse((stage / "image-id").exists())
                self.assertFalse(Path(self.owned_calls()[-1]["config"]).parent.exists())

    def test_observed_identity_is_admitted_before_publication_and_controller(self):
        result = self.caller("stage", environment=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo"})
        self.assertEqual(0, result.returncode, msg=result.stderr)
        calls = self.fixture.calls()
        inspections = [call for call in calls if call["arguments"][:4] == ["image", "inspect", "--format", "{{.Id}}"]]
        self.assertEqual(1, len(inspections))
        inspection = inspections[0]
        self.assertEqual(["image", "inspect", "--format", "{{.Id}}", self.identity.decode().strip()], inspection["arguments"])
        build = next(call for call in calls if call["arguments"][:1] == ["build"])
        self.assertLess(calls.index(build), calls.index(inspection))
        self.assertEqual(build["config"], inspection["config"])
        self.assertEqual([], inspection["config_entries"])
        self.assertEqual(0o700, inspection["config_mode"])
        self.assertFalse(inspection["published_images"])
        self.assertFalse(inspection["controller_returned"])
        self.assertFalse(Path(inspection["config"]).parent.exists())
        staged = [path for path in self.stage.parent.iterdir() if path != self.stage]
        self.assertEqual(1, len(staged))
        self.assertEqual(self.identity, (staged[0] / "image-id").read_bytes())
        self.assertTrue((self.root / "controller-returned").exists())

    def test_unavailable_malformed_or_mismatched_observation_blocks_publication(self):
        cases = (
            (self.identity, 37, 37),
            (self.identity[:-1], 0, 2),
            (self.identity + self.identity, 0, 2),
            (b"mutable:tag\n", 0, 2),
            (self.identity[:-1] + b"\0\n", 0, 2),
            (b"sha256:" + b"c" * 64 + b"\n", 0, 2),
        )
        for body, native_status, expected_status in cases:
            with self.subTest(body=body, native_status=native_status):
                (self.root / "observed-output").write_bytes(body)
                (self.root / "inspect-status").write_text(str(native_status))
                result = self.caller("stage", environment=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo"})
                self._remember(result.stderr)
                self.assertEqual(expected_status, result.returncode, msg=result.stderr)
                self.assertFalse((self.root / "controller-returned").exists())
                for stage in self.stage.parent.iterdir():
                    if stage != self.stage:
                        self.assertFalse((stage / "image-id").exists())
                inspection = self.fixture.calls()[-1]
                self.assertEqual(
                    ["image", "inspect", "--format", "{{.Id}}", self.identity.decode().strip()], inspection["arguments"]
                )
                self.assertFalse(inspection["published_images"])
                self.assertFalse(Path(inspection["config"]).parent.exists())
                if body == b"sha256:" + b"c" * 64 + b"\n":
                    self.assertIn("observed local image identity differs from the built image ID", result.stderr)

    def test_unconfirmed_admission_startup_retains_config_without_publication(self):
        result = self.caller(
            "stage",
            environment=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo", "NMRNET_PACKAGE_ADMISSION_READY_FAULT": "1"},
        )
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertEqual("ready-failure\n", (self.root / "ready-fault").read_text())
        self.assertIn("Cannot confirm package native client retirement", result.stderr)
        self.assertEqual(["build"], [call["arguments"][0] for call in self.owned_calls()])
        self.assertFalse(any(call["arguments"][:3] == ["image", "inspect", "--format"] for call in self.fixture.calls()))
        self.assertFalse((self.root / "controller-returned").exists())
        for stage in self.stage.parent.iterdir():
            if stage != self.stage:
                self.assertFalse((stage / "image-id").exists())
        self.assertTrue(Path(self.owned_calls()[-1]["config"]).parent.is_dir())

    def test_admission_cancellation_joins_client_before_cleanup_and_publication(self):
        for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupt=interrupt):
                ready = self.root / "inspect-ready"
                ready.unlink(missing_ok=True)
                process = subprocess.Popen(
                    ["bash", str(self.root / "tools/packages.sh"), "stage"],
                    env=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo", "NMRNET_PACKAGE_INSPECT_HOLD": "1"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                self.addCleanup(self._retire_failed_probe, process)
                self._wait_path(ready)
                observed = self.fixture.calls()[-1]
                self.assertEqual(
                    ["image", "inspect", "--format", "{{.Id}}", self.identity.decode().strip()], observed["arguments"]
                )
                process.send_signal(interrupt)
                _, stderr = process.communicate(timeout=5)
                self._remember(stderr)
                self.assertEqual(status, process.returncode, msg=stderr)
                self.assertFalse(Path("/proc", str(observed["pid"])).exists())
                self.assertFalse(Path(observed["config"]).parent.exists())
                self.assertFalse((self.root / "controller-returned").exists())
                for stage in self.stage.parent.iterdir():
                    if stage != self.stage:
                        self.assertFalse((stage / "image-id").exists())

    def _wait_path(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), msg="actual fixture phase did not publish its observation")

    def _retire_failed_probe(self, process):
        if process.poll() is None:
            # Hold the unreaped caller identity while retiring its root group
            # first; historical fixture records alone authorize no group kill.
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            deadline = time.monotonic() + 3
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                self.assertLess(time.monotonic(), deadline, msg="failed-probe caller termination was not observed")
                time.sleep(0.01)
            admission = ["image", "inspect", "--format", "{{.Id}}", self.identity.decode().strip()]
            groups = {
                call["process_group"]
                for call in self.fixture.calls()
                if call["arguments"][0] in {"build", "run"} or call["arguments"] == admission
            }
            report = self.root / "report.json"
            if report.exists():
                groups.add(json.loads(report.read_text())["anchor"])
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
                    paths.update(
                        Path(call["config"]).parent
                        for call in self.fixture.calls()
                        if call["process_group"] == group and call["config"] is not None
                    )
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

    def test_failure_rescue_preserves_another_callers_live_native_group(self):
        earlier = subprocess.Popen(
            ["bash", str(self.root / "tools/packages.sh"), "apply"],
            env=self.apply_environment() | {"NMRNET_PACKAGE_HOLD": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self._retire_failed_probe, earlier)
        self._wait_path(self.root / "client-ready")
        earlier_call = self.fixture.calls()[-1]
        current = subprocess.Popen(
            ["bash", str(self.root / "tools/packages.sh"), "stage"],
            env=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo", "NMRNET_PACKAGE_INSPECT_HOLD": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.addCleanup(self._retire_failed_probe, current)
        self._wait_path(self.root / "inspect-ready")
        current_call = self.fixture.calls()[-1]
        self._retire_failed_probe(current)
        self.assertEqual(-signal.SIGKILL, current.returncode)
        self.assertFalse(Path("/proc", str(current_call["pid"])).exists())
        self.assertTrue(Path(current_call["config"]).parent.is_dir())
        self.assertIsNone(earlier.poll())
        self.assertTrue(Path("/proc", str(earlier_call["pid"])).exists())
        self.assertEqual(earlier_call["process_group"], os.getpgid(earlier_call["pid"]))
        self.assertTrue(Path(earlier_call["config"]).parent.is_dir())
        self._retire_failed_probe(earlier)
        self.assertEqual(-signal.SIGKILL, earlier.returncode)
        self.assertFalse(Path("/proc", str(earlier_call["pid"])).exists())
        self.assertTrue(Path(earlier_call["config"]).parent.is_dir())

    def test_apply_cancellation_joins_client_before_removing_config(self):
        for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupt=interrupt):
                ready = self.root / "client-ready"
                ready.unlink(missing_ok=True)
                process = subprocess.Popen(
                    ["bash", str(self.root / "tools/packages.sh"), "apply"],
                    env=self.apply_environment() | {"NMRNET_PACKAGE_HOLD": "1"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                self.addCleanup(self._retire_failed_probe, process)
                self._wait_path(ready)
                observed = self.owned_calls()[-1]
                process.send_signal(interrupt)
                _, stderr = process.communicate(timeout=5)
                self._remember(stderr)
                self.assertEqual(status, process.returncode, msg=stderr)
                self.assertFalse(Path("/proc", str(observed["pid"])).exists())
                self.assertFalse(Path(observed["config"]).parent.exists())

    def test_full_unread_review_stdout_cancellation_finishes_before_drain(self):
        for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupt=interrupt):
                for name in ("controller-returned", "report.json"):
                    (self.root / name).unlink(missing_ok=True)
                process = subprocess.Popen(
                    ["bash", str(self.root / "tools/packages.sh"), "stage"],
                    env=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo", "NMRNET_PACKAGE_FILL_STDOUT": "1"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                self.addCleanup(self._retire_failed_probe, process)
                self._wait_path(self.root / "report.json")
                report = json.loads((self.root / "report.json").read_text())
                controller = int((self.root / "controller-returned").read_text())
                self.assertFalse(Path("/proc", str(controller)).exists())
                children = Path("/proc", str(report["anchor"]), "task", str(report["anchor"]), "children")
                deadline = time.monotonic() + 5
                publisher = None
                while publisher is None and time.monotonic() < deadline:
                    for child in children.read_text().split():
                        # The anchor's short-lived sleeps can finish between
                        # enumeration and inspection; the blocked writer stays.
                        try:
                            command = Path("/proc", child, "cmdline").read_bytes()
                            if b"Review %s, then apply with make packages/lock/apply STAGE=%s" in command:
                                channel = Path("/proc", child, "wchan").read_text().strip()
                                if channel in {"pipe_write", "anon_pipe_write"}:
                                    publisher = int(child)
                                    break
                        except (FileNotFoundError, ProcessLookupError):
                            continue
                    if publisher is None:
                        time.sleep(0.01)
                self.assertIsNotNone(publisher, msg="actual Review publisher pipe-write wait was not observed")
                self.assertEqual(report["anchor"], os.getpgid(publisher))
                self.assertIsNone(process.poll())
                process.send_signal(interrupt)
                process.wait(timeout=5)  # Deliberately do not drain either pipe first.
                self.assertEqual(status, process.returncode)
                self.assertFalse(Path("/proc", str(publisher)).exists())
                self.assertFalse(Path(self.owned_calls()[-1]["config"]).parent.exists())
                _, stderr = process.communicate(timeout=2)
                self._remember(stderr)

    def test_full_unread_mismatch_stderr_reports_after_cleanup_and_cancels_before_drain(self):
        (self.root / "observed-output").write_bytes(b"sha256:" + b"c" * 64 + b"\n")
        record = self.stage / "image-id"
        before_record = record.read_bytes(), record.stat().st_ino
        diagnostic = "Cannot record package resolver image: observed local image identity differs from the built image ID.\n"
        source = (self.root / "tools/packages.sh").read_text()
        payload = source.split("if inference_native 'package client cleanup reporting' untimed stdio bash -eu -c '", 1)[1]
        payload = payload.split("' bash ", 1)[0]
        for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupt=interrupt):
                (self.root / "report.json").unlink(missing_ok=True)
                before_calls = len(self.fixture.calls())
                read_fd, write_fd = os.pipe()
                process = None
                try:
                    os.set_blocking(write_fd, False)
                    while True:
                        try:
                            os.write(write_fd, b"x" * 4096)
                        except BlockingIOError:
                            break
                    os.set_blocking(write_fd, True)
                    process = subprocess.Popen(
                        ["bash", str(self.root / "tools/packages.sh"), "stage"],
                        env=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo"},
                        stdout=subprocess.PIPE,
                        stderr=write_fd,
                        start_new_session=True,
                    )
                    os.close(write_fd)
                    write_fd = None
                    deadline = time.monotonic() + 5
                    publisher = None
                    observed = []
                    while publisher is None and time.monotonic() < deadline:
                        pending = [process.pid]
                        observed = []
                        while pending:
                            identifier = pending.pop()
                            with suppress(FileNotFoundError, ProcessLookupError):
                                children = Path("/proc", str(identifier), "task", str(identifier), "children")
                                found = [int(value) for value in children.read_text().split()]
                                pending.extend(found)
                                observed.extend(found)
                        for identifier in observed:
                            with suppress(FileNotFoundError, ProcessLookupError):
                                command = Path("/proc", str(identifier), "cmdline").read_bytes().split(b"\0")
                                channel = Path("/proc", str(identifier), "wchan").read_text().strip()
                                if command[1:4] == [b"-eu", b"-c", payload.encode()] and channel in {
                                    "pipe_write",
                                    "anon_pipe_write",
                                }:
                                    self.assertEqual(diagnostic.encode(), command[5])
                                    publisher = identifier
                        if publisher is None:
                            time.sleep(0.01)
                    self.assertIsNotNone(publisher, msg="Actual mismatch publisher pipe-write wait was not observed")
                    anchor = os.getpgid(publisher)
                    # Give the existing bounded rescue only this observed
                    # candidate; it still checks session, signature and parent.
                    (self.root / "report.json").write_text(json.dumps({"anchor": anchor}))
                    self.assertIn(anchor, observed)
                    self.assertNotEqual(anchor, publisher)
                    anchors = []
                    for identifier in observed:
                        with suppress(FileNotFoundError, ProcessLookupError):
                            command = Path("/proc", str(identifier), "cmdline").read_bytes()
                            self.assertNotIn(str(self.root / "bin/docker").encode(), command.split(b"\0"))
                            if b"receipt=$1 parent=$2 budget=$3 timed_program=$4" in command:
                                anchors.append(identifier)
                    self.assertEqual([anchor], anchors)
                    calls = [call for call in self.fixture.calls()[before_calls:] if call["config"] is not None]
                    self.assertEqual(["build", "image"], [call["arguments"][0] for call in calls])
                    self.assertEqual(
                        ["image", "inspect", "--format", "{{.Id}}", self.identity.decode().strip()], calls[1]["arguments"]
                    )
                    for call in calls:
                        self.assertFalse(Path("/proc", str(call["pid"])).exists())
                        self.assertFalse(Path(call["config"]).parent.exists(), msg="Mismatch preceded quiet config cleanup")
                    self.assertEqual([], calls[1]["published_images"])
                    self.assertFalse((self.root / "controller-returned").exists())
                    self.assertEqual(before_record, (record.read_bytes(), record.stat().st_ino))
                    for stage in self.stage.parent.iterdir():
                        if stage != self.stage:
                            self.assertFalse((stage / "image-id").exists())
                    process.send_signal(interrupt)
                    self.assertEqual(status, process.wait(timeout=5))
                    self.assertFalse(Path("/proc", str(publisher)).exists())
                    self.assertFalse(Path("/proc", str(anchor)).exists())
                    self.assertEqual(b"", process.stdout.read())
                    self.assertEqual(before_record, (record.read_bytes(), record.stat().st_ino))
                finally:
                    try:
                        if process is not None:
                            # Keep the reader open until the existing owner-
                            # gated rescue has finished; do not drain stderr.
                            self._retire_failed_probe(process)
                            process.communicate(timeout=3)
                    finally:
                        if write_fd is not None:
                            os.close(write_fd)
                        os.close(read_fd)


def fake_command(name):
    """Implement only finite clients and the specific removal/receipt faults."""
    root = Path(os.environ["NMRNET_PACKAGE_FIXTURE_ROOT"])
    arguments = sys.argv[1:]
    if name == "rm":
        destination = Path(arguments[-1])
        fault = os.environ.get("NMRNET_PACKAGE_RM_FAULT")
        if arguments[:2] == ["-rf", "--"] and destination.name.startswith("nmrnet-docker-client.") and fault:
            if fault == "delete":
                subprocess.run(["/bin/rm", *arguments], check=True)
            return 41
        os.execv("/bin/rm", ["rm", *arguments])
    if name == "mv":
        destination = Path(arguments[-1])
        if destination.name == "status.ready" and (
            os.environ.get("NMRNET_PACKAGE_READY_FAULT")
            or (os.environ.get("NMRNET_PACKAGE_ADMISSION_READY_FAULT") and (root / "build-returned").exists())
        ):
            with (root / "ready-fault").open("a") as stream:
                stream.write("ready-failure\n")
            return 47
        subprocess.run(["/bin/mv", *arguments], check=True)
        if destination.name == "status.ready" and (root / "controller-returned").exists():
            pending = root / "report.pending"
            pending.write_text(json.dumps({"anchor": int(destination.read_text())}))
            pending.rename(root / "report.json")
        return 0
    config = None
    if arguments[:1] == ["--config"]:
        config = Path(arguments[1])
        arguments = arguments[2:]
    if arguments[:2] != ["--host", "unix:///var/run/docker.sock"]:
        return 97
    arguments = arguments[2:]
    environment = {
        name: value
        for name, value in os.environ.items()
        if name.startswith(("DOCKER_", "BUILDX_")) or name.lower().endswith("_proxy")
    }
    observed = {
        "arguments": arguments,
        "config": str(config) if config else None,
        "config_entries": sorted(path.name for path in config.iterdir()) if config else None,
        "config_mode": stat.S_IMODE(config.stat().st_mode) if config else None,
        "environment": environment,
        "pid": os.getpid(),
        "process_group": os.getpgrp(),
    }
    if arguments == ["image", "inspect", "--format", "{{.Id}}", "sha256:" + "b" * 64]:
        observed["published_images"] = [
            str(path) for path in (root / ".work/packages-stage").glob("lock.*/image-id") if path.parent.name != "lock.fixture"
        ]
        observed["controller_returned"] = (root / "controller-returned").exists()
    record_engine_call(Path(os.environ["NMRNET_FAKE_ENGINE_LOG"]), observed)
    if arguments == ["image", "inspect", "--format", "{{.Id}}", "sha256:" + "b" * 64]:
        if os.environ.get("NMRNET_PACKAGE_INSPECT_HOLD"):
            for interrupt in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                signal.signal(interrupt, signal.SIG_IGN)
            (root / "inspect-ready").write_text("ready\n")
            while True:
                time.sleep(1)
        os.write(1, (root / "observed-output").read_bytes())
        return int((root / "inspect-status").read_text())
    if arguments[:2] == ["image", "inspect"]:
        return 0
    if os.environ.get("NMRNET_PACKAGE_HOLD"):
        for interrupt in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(interrupt, signal.SIG_IGN)
        (root / "client-ready").write_text("ready\n")
        while True:
            time.sleep(1)
    if arguments[0] == "build":
        os.write(1, (root / "build-output").read_bytes())
        (root / "build-returned").write_text("ready\n")
    elif arguments[0] == "run":
        os.write(1, b"package output\n")
        os.write(2, b"package diagnostic\n")
    else:
        return 97
    return int((root / "native-status").read_text())


def fake_controller():
    """Return before the real Review publisher, optionally filling its pipe."""
    root = Path(os.environ["NMRNET_PACKAGE_FIXTURE_ROOT"])
    if os.environ.get("NMRNET_PACKAGE_FILL_STDOUT"):
        capacity = fcntl.fcntl(1, fcntl.F_GETPIPE_SZ)
        if os.write(1, b"x" * capacity) != capacity:
            return 98
    (root / "controller-returned").write_text(str(os.getpid()))
    return 0


if __name__ == "__main__":
    name = Path(sys.argv[0]).name
    if name in {"docker", "rm", "mv"}:
        sys.exit(fake_command(name))
    if sys.argv[1:] == ["controller"]:
        sys.exit(fake_controller())
    unittest.main()
