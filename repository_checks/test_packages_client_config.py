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

from repository_checks.fixtures.containers.fixture import LauncherFixture


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
        (self.root / "tools/docker.sh").write_text(
            "#!/bin/bash\n"
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

    def _wait_path(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), msg="actual fixture phase did not publish its observation")

    def _retire_failed_probe(self, process):
        if process.poll() is None:
            for call in self.owned_calls():
                with suppress(ProcessLookupError):
                    os.killpg(call["process_group"], signal.SIGKILL)
            report = self.root / "report.json"
            if report.exists():
                with suppress(ProcessLookupError):
                    os.killpg(json.loads(report.read_text())["anchor"], signal.SIGKILL)
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)

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
        if destination.name == "status.ready" and os.environ.get("NMRNET_PACKAGE_READY_FAULT"):
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
    with Path(os.environ["NMRNET_FAKE_ENGINE_LOG"]).open("a") as stream:
        stream.write(json.dumps(observed) + "\n")
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
