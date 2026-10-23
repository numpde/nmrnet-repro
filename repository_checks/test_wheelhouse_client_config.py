#!/usr/bin/env python3
"""Observe offline wheelhouse client ownership without acquisition or builds.

The actual shared native helper owns child lifetime. Finite executable clients
observe configuration, grants, status and cancellation inside the checker lane;
the download controller remains a separate owner and is exercised only in PRINT.
"""

from tools.container_guard import require_container

require_container()

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


class WheelhouseClientTests(unittest.TestCase):
    def test_make_resolves_a_literal_relative_path_from_its_working_directory(self):
        cache = self.root / "shared $(shell touch sentinel) cache"
        cache.mkdir(parents=True)
        result = self.fixture.make(
            "packages/wheelhouse/check",
            f"WHEELHOUSE=./{cache.name}",
            additions={"NMRNET_WHEELHOUSE_FIXTURE_ROOT": str(self.root)},
        )
        self._remember(result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "sentinel").exists())
        worker = next(call["arguments"] for call in self.fixture.calls() if "tools.wheelhouse" in call["arguments"])
        self.assertIn(f"type=bind,src={cache},dst=/wheelhouse,readonly", worker)
        self.assertEqual(worker[worker.index("--pool") + 1], "/wheelhouse")

    def test_make_without_a_terminal_requires_a_path_before_engine_effects(self):
        result = subprocess.run(
            ["make", "--no-print-directory", "-C", str(self.root), "packages/image"],
            env=self.environment | {"WHEELHOUSE": str(self.root / ".work/wheelhouse")},
            input="/piped/input/is/not/a/selection\n",
            start_new_session=True,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("WHEELHOUSE must be set on the make command line", result.stderr)
        self.assertFalse(self.fixture.calls())

    def test_preparation_print_isolated_mounts_and_source_admission(self):
        lock = self.root / "requirements/preparation.lock"
        lock.write_text("fixture==1\n")
        (self.root / ".work/preparation").mkdir()
        (self.root / "requirements/unicore-source.json").unlink()
        environment = self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo"}
        for action in ("download", "check"):
            result = self.caller("--print", action, "preparation", environment=environment)
            self.assertEqual(0, result.returncode, result.stderr)
            commands = [shlex.split(line) for line in result.stdout.splitlines()]
            command = next(command for command in commands if "tools.wheelhouse" in command)
            mounts = [command[index + 1] for index, value in enumerate(command) if value == "--mount"]
            inputs = [mount for mount in mounts if ",dst=/input/" in mount]
            self.assertEqual([f"type=bind,src={lock},dst=/input/preparation.lock,readonly"], inputs)
            self.assertNotIn("--source", command)
            self.assertEqual("preparation", command[command.index("--profile") + 1])
            self.assertEqual("/wheelhouse", command[command.index("--pool") + 1])
            if action == "check":
                self.assertEqual("none", command[command.index("--network") + 1])
                self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
            else:
                self.assertEqual(
                    [f"type=bind,src={self.root}/.work/wheelhouse,dst=/wheelhouse"],
                    [mount for mount in mounts if not mount.endswith(",readonly")],
                )
            self.assertFalse(self.fixture.calls())
        lock.unlink()
        for linked in (False, True):
            if linked:
                lock.symlink_to(self.root / "requirements/inference.lock")
            result = self.caller("check", "preparation")
            self.assertEqual(2, result.returncode, result.stderr)
            self.assertIn("regular file", result.stderr)
            self.assertFalse(self.fixture.calls())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = LauncherFixture(self.root)
        self.addCleanup(self.fixture.dispose)
        (self.root / ".work/wheelhouse/.complete").unlink()
        self.assertFalse((self.root / ".work/wheelhouse/.complete").exists())
        self.retained = set()
        self.addCleanup(self._remove_retained)
        self.environment = self.fixture.environment | {"NMRNET_WHEELHOUSE_FIXTURE_ROOT": str(self.root)}
        for name in ("docker", "rm", "mv"):
            target = self.root / "bin" / name
            target.unlink(missing_ok=True)
            target.symlink_to(Path(__file__).resolve())
        (self.root / "native-status").write_text("0")

    def _remove_retained(self):
        for path in self.retained:
            subprocess.run(["/bin/rm", "-rf", "--", str(path)], check=True)

    def _remember(self, stderr):
        for path in re.findall(r"/tmp/nmrnet-(?:docker-client|inference-native)\.[A-Za-z0-9]+", stderr):
            self.retained.add(Path(path))

    def caller(self, *arguments, environment=None):
        result = subprocess.run(
            ["bash", str(self.root / "tools/wheelhouse.sh"), *arguments],
            env=self.environment if environment is None else environment,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
        self._remember(result.stderr)
        return result

    def test_print_and_rejected_inputs_do_not_prepare_or_dispatch_client(self):
        ambient = self.root / "ambient-owned-name"
        ambient.mkdir()
        environment = self.environment | {"docker_client_directory": str(ambient), "NMRNET_WLAN_INTERFACE_INPUT": "lo"}
        for arguments in ((), ("--print",), ("unknown",), ("check", "extra"), ("--print", "check"), ("--print", "download")):
            with self.subTest(arguments=arguments):
                result = self.caller(*arguments, environment=environment)
                # silent-defaults: allow-contract -- Only the two valid PRINT forms expect success; the four rejected argument forms expect status2, with no engine calls in either case; proof=repository_checks/test_wheelhouse_client_config.py::WheelhouseClientTests.test_print_and_rejected_inputs_do_not_prepare_or_dispatch_client
                self.assertEqual(
                    0 if len(arguments) == 2 and arguments[0] == "--print" else 2, result.returncode, msg=result.stderr
                )
                self.assertTrue(ambient.is_dir())
                self.assertFalse(self.fixture.calls())
        for name in ("inference.lock", "unicore-source.json"):
            path = self.root / "requirements" / name
            body = path.read_bytes()
            path.unlink()
            result = self.caller("check", environment=environment)
            self.assertEqual(2, result.returncode, msg=result.stderr)
            self.assertIn("Wheelhouse input must be a regular file", result.stderr)
            self.assertFalse(self.fixture.calls())
            path.write_bytes(body)
        wheelhouse = self.root / ".work/wheelhouse"
        wheelhouse.rename(wheelhouse.with_name("saved-wheelhouse"))
        result = self.caller("check", environment=environment)
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertIn(f"Cannot check wheelhouse: {self.root}/.work/wheelhouse is missing.", result.stderr)
        self.assertTrue(ambient.is_dir())
        self.assertFalse(self.fixture.calls())

    def test_check_uses_empty_private_config_and_fixed_client_environment(self):
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
            self.environment | dict.fromkeys(forbidden, "poison") | {"DOCKER_CONFIG": str(poison), "DOCKER_BUILDKIT": "0"}
        )
        result = self.caller("check", environment=environment)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        calls = self.fixture.calls()
        self.assertEqual(1, len(calls))
        call = calls[0]
        self.assertEqual([], call["config_entries"])
        self.assertEqual(0o700, call["config_mode"])
        self.assertEqual("1", call["environment"]["DOCKER_BUILDKIT"])
        self.assertTrue(set(forbidden).isdisjoint(call["environment"]))
        self.assertNotEqual(str(poison), call["config"])
        self.assertFalse(Path(call["config"]).parent.exists())
        self.assertTrue((poison / "config.json").is_file())
        printed = self.caller("--print", "check")
        self.assertEqual(0, printed.returncode, msg=printed.stderr)
        arguments = shlex.split(printed.stdout)
        self.assertEqual(call["arguments"], arguments[arguments.index("run") :])
        self.assertEqual(
            [
                "run",
                "--rm",
                "--network",
                "none",
                "--pull",
                "never",
                "--read-only",
                "--env",
                "NMRNET_IN_CONTAINER=1",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--cpus",
                "2",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--pids-limit",
                "64",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,noexec,size=64m",
                "--workdir",
                "/workspace",
            ],
            call["arguments"][:27],
        )
        mounts = call["arguments"][27:39]
        self.assertEqual(
            [
                "--mount",
                f"type=bind,src={self.root}/tools/wheelhouse.py,dst=/workspace/tools/wheelhouse.py,readonly",
                "--mount",
                f"type=bind,src={self.root}/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly",
                "--mount",
                f"type=bind,src={self.root}/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly",
                "--mount",
                f"type=bind,src={self.root}/requirements/inference.lock,dst=/input/inference.lock,readonly",
                "--mount",
                f"type=bind,src={self.root}/requirements/unicore-source.json,dst=/input/unicore-source.json,readonly",
                "--mount",
                f"type=bind,src={self.root}/.work/wheelhouse,dst=/wheelhouse,readonly",
            ],
            mounts,
        )
        self.assertEqual(
            [
                "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c",
                "python3",
                "-B",
                "-m",
                "tools.wheelhouse",
                "check",
                "--profile",
                "inference",
                "--lock",
                "/input/inference.lock",
                "--source",
                "/input/unicore-source.json",
                "--pool",
                "/wheelhouse",
            ],
            call["arguments"][39:],
        )

    def test_native_output_and_failure_status_are_preserved(self):
        for status in (0, 37):
            with self.subTest(status=status):
                (self.root / "native-status").write_text(str(status))
                result = self.caller("check")
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertEqual("wheelhouse output\n", result.stdout)
                self.assertEqual("wheelhouse diagnostic\n", result.stderr)
                self.assertFalse(Path(self.fixture.calls()[-1]["config"]).parent.exists())

    def test_cleanup_failure_changes_only_success_and_preserves_attempt_truth(self):
        for status, delete in ((0, False), (37, False), (0, True)):
            with self.subTest(status=status, delete=delete):
                (self.root / "native-status").write_text(str(status))
                # silent-defaults: allow-contract -- This cleanup table selects delete-then-fail or failure without deletion; the directory effect is checked independently; proof=repository_checks/test_wheelhouse_client_config.py::WheelhouseClientTests.test_cleanup_failure_changes_only_success_and_preserves_attempt_truth
                result = self.caller(
                    "check", environment=self.environment | {"NMRNET_WHEELHOUSE_RM_FAULT": "delete" if delete else "retain"}
                )
                # silent-defaults: allow-contract -- The declared primary failure 37 wins; otherwise cleanup failure expects 41, with attempted-path wording and actual deletion checked separately; proof=repository_checks/test_wheelhouse_client_config.py::WheelhouseClientTests.test_cleanup_failure_changes_only_success_and_preserves_attempt_truth
                self.assertEqual(status or 41, result.returncode, msg=result.stderr)
                self.assertIn("Cannot confirm wheelhouse Docker client configuration removal; attempted path", result.stderr)
                self.assertNotIn("configuration retained", result.stderr)
                directory = Path(self.fixture.calls()[-1]["config"]).parent
                self.assertEqual(not delete, directory.exists())
                self.retained.add(directory)

    def test_unconfirmed_startup_keeps_config_and_never_starts_another_owner(self):
        result = self.caller("check", environment=self.environment | {"NMRNET_WHEELHOUSE_READY_FAULT": "1"})
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertEqual("ready-failure\n", (self.root / "ready-fault").read_text())
        self.assertIn("Cannot confirm wheelhouse native client retirement", result.stderr)
        self.assertFalse(self.fixture.calls())
        directories = [path for path in self.retained if path.name.startswith("nmrnet-docker-client.")]
        self.assertEqual(1, len(directories))
        self.assertTrue(directories[0].is_dir())

    def _retire_failed_probe(self, process):
        if process.poll() is None:
            for call in self.fixture.calls():
                with suppress(ProcessLookupError):
                    os.killpg(call["process_group"], signal.SIGKILL)
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)

    def test_cancellation_joins_client_before_removing_config(self):
        for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupt=interrupt):
                ready = self.root / "client-ready"
                ready.unlink(missing_ok=True)
                process = subprocess.Popen(
                    ["bash", str(self.root / "tools/wheelhouse.sh"), "check"],
                    env=self.environment | {"NMRNET_WHEELHOUSE_HOLD": "1"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                self.addCleanup(self._retire_failed_probe, process)
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), msg="actual client did not publish its ready observation")
                observed = self.fixture.calls()[-1]
                process.send_signal(interrupt)
                _, stderr = process.communicate(timeout=5)
                self._remember(stderr)
                self.assertEqual(status, process.returncode, msg=stderr)
                self.assertFalse(Path("/proc", str(observed["pid"])).exists())
                self.assertFalse(Path(observed["config"]).parent.exists())


def fake_command(name):
    """Implement a finite native client and precise removal/receipt faults."""
    root = Path(os.environ["NMRNET_WHEELHOUSE_FIXTURE_ROOT"])
    arguments = sys.argv[1:]
    if name == "rm":
        destination = Path(arguments[-1])
        fault = os.environ.get("NMRNET_WHEELHOUSE_RM_FAULT")
        if arguments[:2] == ["-rf", "--"] and destination.name.startswith("nmrnet-docker-client.") and fault:
            if fault == "delete":
                subprocess.run(["/bin/rm", *arguments], check=True)
            return 41
        os.execv("/bin/rm", ["rm", *arguments])
    if name == "mv":
        destination = Path(arguments[-1])
        if destination.name == "status.ready" and os.environ.get("NMRNET_WHEELHOUSE_READY_FAULT"):
            with (root / "ready-fault").open("a") as stream:
                stream.write("ready-failure\n")
            return 47
        os.execv("/bin/mv", ["mv", *arguments])
    if arguments[:3:2] != ["--config", "--host"] or arguments[3] != "unix:///var/run/docker.sock":
        return 97
    config = Path(arguments[1])
    arguments = arguments[4:]
    observed = {
        "arguments": arguments,
        "config": str(config),
        "config_entries": sorted(path.name for path in config.iterdir()),
        "config_mode": stat.S_IMODE(config.stat().st_mode),
        "environment": {
            name: value
            for name, value in os.environ.items()
            if name.startswith(("DOCKER_", "BUILDX_")) or name.lower().endswith("_proxy")
        },
        "pid": os.getpid(),
        "process_group": os.getpgrp(),
    }
    record_engine_call(Path(os.environ["NMRNET_FAKE_ENGINE_LOG"]), observed)
    if os.environ.get("NMRNET_WHEELHOUSE_HOLD"):
        for interrupt in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(interrupt, signal.SIG_IGN)
        (root / "client-ready").write_text("ready\n")
        while True:
            time.sleep(1)
    if arguments[:1] != ["run"]:
        return 97
    os.write(1, b"wheelhouse output\n")
    os.write(2, b"wheelhouse diagnostic\n")
    return int((root / "native-status").read_text())


if __name__ == "__main__":
    name = Path(sys.argv[0]).name
    if name in {"docker", "rm", "mv"}:
        sys.exit(fake_command(name))
    unittest.main()
