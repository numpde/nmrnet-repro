"""Observe inference's owned client config independently of caller defaults."""

from tools.container_guard import require_container

require_container()

import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path

from repository_checks.fixtures.containers.fixture import LauncherFixture


class InferenceBuildInputAdmissionTests(unittest.TestCase):
    def test_unsafe_requirement_leaf_never_reaches_engine(self):
        for name in ("inference.lock", "unicore-source.json"):
            for kind in ("symlink", "missing", "directory", "fifo"):
                with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                    fixture = LauncherFixture(Path(directory))
                    candidate = fixture.root / "requirements" / name
                    candidate.unlink()
                    if kind == "symlink":
                        outside = fixture.root / "outside-requirement"
                        outside.write_text("outside-file-must-not-be-mounted\n")
                        candidate.symlink_to(outside)
                    elif kind == "directory":
                        candidate.mkdir()
                    elif kind == "fifo":
                        os.mkfifo(candidate)
                    result = fixture.run("inference_image.sh", "build")
                    self.assertEqual(2, result.returncode, msg=result.stderr)
                    self.assertIn(name, result.stderr)
                    self.assertEqual([], fixture.calls(), msg="Unsafe requirements reached an engine dispatch")

    def test_unsafe_requirements_parent_never_reaches_engine(self):
        for kind in ("symlink", "missing", "file", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                requirements = fixture.root / "requirements"
                saved = fixture.root / "outside-requirements"
                requirements.rename(saved)
                if kind == "symlink":
                    requirements.symlink_to(saved, target_is_directory=True)
                elif kind == "file":
                    requirements.write_text("not-a-directory\n")
                elif kind == "fifo":
                    os.mkfifo(requirements)
                result = fixture.run("inference_image.sh", "build")
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertIn("requirements", result.stderr)
                self.assertEqual([], fixture.calls(), msg="An unsafe requirements ancestor reached the engine")


class InferenceClientConfigTests(unittest.TestCase):
    def caller_configuration(self, root):
        home = root / "caller"
        configuration = home / ".docker"
        configuration.mkdir(parents=True)
        marker = "synthetic-caller-auth-and-proxy"
        body = json.dumps({"auths": {"registry.invalid": {"auth": marker}}, "proxies": {"default": {"httpProxy": marker}}})
        (configuration / "config.json").write_text(body)
        override = root / "caller-override"
        override.mkdir()
        (override / "config.json").write_text(body)
        return {"HOME": str(home), "DOCKER_CONFIG": str(override)}, (configuration, override)

    def assert_owned_configuration(self, fixture, caller_paths, *, retired=True):
        calls = fixture.calls()
        self.assertTrue(calls, msg="The actual launcher never reached the engine double")
        configurations = [call["client_config"] for call in calls]
        self.assertTrue(all(configuration is not None for configuration in configurations))
        paths = {configuration["path"] for configuration in configurations}
        self.assertEqual(1, len(paths), msg="Preflight and execution did not share one owned client authority")
        path = Path(paths.pop())
        self.assertNotIn(path, caller_paths)
        self.assertEqual("config", path.name)
        self.assertEqual(Path("/tmp"), path.parent.parent)
        self.assertTrue(path.parent.name.startswith("nmrnet-docker-client."))
        for configuration in configurations:
            self.assertEqual("directory", configuration["kind"])
            self.assertEqual(os.getuid(), configuration["uid"])
            self.assertEqual(0o700, configuration["mode"])
            self.assertEqual([], configuration["entries"])
        self.assertEqual(not retired, path.exists())
        for caller in caller_paths:
            self.assertTrue((caller / "config.json").is_file())
        return path

    def test_print_selects_inert_configuration_without_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            environment, _ = self.caller_configuration(fixture.root)
            result = fixture.run("inference_image.sh", "--print", "import", additions=environment)
            self.assertEqual(0, result.returncode, msg=result.stderr)
            arguments = shlex.split(result.stdout)
            self.assertEqual("/tmp/nmrnet-docker-client.PRINT/config", arguments[arguments.index("--config") + 1])
            self.assertEqual([], fixture.calls())

    def test_unpublished_image_record_is_removed_after_publication_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            environment, caller_paths = self.caller_configuration(fixture.root)
            record = fixture.root / ".work/inference/image-id"
            before = record.read_bytes(), record.stat().st_ino
            failing_publication = '() { case "$*" in */image-id.*) return 11;; *) command mv "$@";; esac; }'
            result = fixture.run("inference_image.sh", "build", additions=environment | {"BASH_FUNC_mv%%": failing_publication})
            self.assertEqual(11, result.returncode, msg=result.stderr)
            self.assertEqual(before, (record.read_bytes(), record.stat().st_ino))
            self.assertEqual({"image-id"}, {path.name for path in record.parent.iterdir()})
            self.assert_owned_configuration(fixture, caller_paths)

    def test_build_and_import_select_empty_owned_configuration_then_retire_it(self):
        for action in ("build", "import"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                environment, caller_paths = self.caller_configuration(fixture.root)
                environment |= dict.fromkeys(
                    (
                        "DOCKER_HOST",
                        "DOCKER_CONTEXT",
                        "DOCKER_TLS",
                        "DOCKER_TLS_VERIFY",
                        "DOCKER_CERT_PATH",
                        "DOCKER_AUTH_CONFIG",
                        "DOCKER_CUSTOM_HEADERS",
                        "DOCKER_API_VERSION",
                        "DOCKER_DEFAULT_PLATFORM",
                        "BUILDX_BUILDER",
                        "BUILDX_CONFIG",
                        "DOCKER_BUILDKIT",
                        "HTTP_PROXY",
                        "HTTPS_PROXY",
                        "ALL_PROXY",
                        "NO_PROXY",
                        "http_proxy",
                        "https_proxy",
                        "all_proxy",
                        "no_proxy",
                    ),
                    "synthetic-ambient-client-setting",
                )
                result = fixture.run("inference_image.sh", action, additions=environment)
                self.assertEqual(0, result.returncode, msg=result.stderr)
                self.assert_owned_configuration(fixture, caller_paths)
                for call in fixture.calls():
                    self.assertEqual({"DOCKER_BUILDKIT": "1"}, call["environment"])
                if action == "build":
                    commands = [call["arguments"][0] for call in fixture.calls()]
                    self.assertIn("build", commands)
                    self.assertIn("run", commands)

    def test_failed_build_and_expired_import_retire_owned_configuration(self):
        cases = [("build", {"NMRNET_FAKE_BUILD_STATUS": "7"}, 7), ("import", {"NMRNET_FAKE_HOLD_MODE": "eof"}, 124)]
        for action, additions, expected in cases:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                environment, caller_paths = self.caller_configuration(fixture.root)
                arguments = ("--timeout", "0.1", action) if action == "import" else (action,)
                result = fixture.run("inference_image.sh", *arguments, additions=environment | additions)
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assert_owned_configuration(fixture, caller_paths)

    def test_cancellation_retires_configuration_after_joining_native_client(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            environment, caller_paths = self.caller_configuration(fixture.root)
            process = subprocess.Popen(
                ["bash", str(fixture.root / "tools/inference_image.sh"), "import"],
                env=fixture.environment | environment | {"NMRNET_FAKE_HOLD_MODE": "eof"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 2
                while len(fixture.calls()) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(2, len(fixture.calls()), msg="The import client did not dispatch before cancellation")
                process.send_signal(signal.SIGTERM)
                _, stderr = process.communicate(timeout=2)
                self.assertEqual(143, process.returncode, msg=stderr)
                self.assert_owned_configuration(fixture, caller_paths)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=2)

    def test_cleanup_failure_keeps_primary_status_and_retained_configuration(self):
        for effect in ("before", "after"):
            failing_removal = (
                '() { case "$*" in *nmrnet-docker-client.*) '
                + ('command rm "$@"; ' if effect == "after" else "")
                + 'return 9;; *) command rm "$@";; esac; }'
            )
            for primary, expected in ((0, 9), (7, 7), (130, 130)):
                with self.subTest(effect=effect, primary=primary), tempfile.TemporaryDirectory() as directory:
                    fixture = LauncherFixture(Path(directory))
                    environment, caller_paths = self.caller_configuration(fixture.root)
                    additions = environment | {"BASH_FUNC_rm%%": failing_removal}
                    if primary:
                        additions["NMRNET_FAKE_BUILD_STATUS"] = str(primary)
                    result = fixture.run("inference_image.sh", "build", additions=additions)
                    self.assertEqual(expected, result.returncode, msg=result.stderr)
                    self.assertIn("Cannot confirm owned Docker client configuration removal; attempted path", result.stderr)
                    self.assertNotIn("configuration; retained", result.stderr)
                    path = self.assert_owned_configuration(fixture, caller_paths, retired=effect == "after")
                    diagnostic = next(
                        line for line in result.stderr.splitlines() if line.startswith("Cannot confirm owned Docker")
                    )
                    self.assertEqual(
                        f"Cannot confirm owned Docker client configuration removal; attempted path {path.parent} (status 9).",
                        diagnostic,
                    )
                    if effect == "before":
                        path.rmdir()
                        path.parent.rmdir()

    def test_unconfirmed_actual_native_retirement_stops_and_retains_owned_inputs(self):
        deny_retirement = (
            '() { if [[ "$1" == -KILL ]]; then '
            'printf "%s\\n" "$leader" "$drain" "$scratch" "$docker_client_directory" "$capture_directory" '
            '> "$NMRNET_RETIREMENT_OBSERVATION"; return 1; fi; builtin kill "$@"; }'
        )
        for action in ("build", "import"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                environment, caller_paths = self.caller_configuration(fixture.root)
                observation = fixture.root / "unconfirmed-retirement"
                record = fixture.root / ".work/inference/image-id"
                before = record.read_bytes(), record.stat().st_ino
                try:
                    result = fixture.run(
                        "inference_image.sh",
                        action,
                        additions=environment
                        | {
                            "BASH_FUNC_kill%%": deny_retirement,
                            "NMRNET_RETIREMENT_OBSERVATION": str(observation),
                        },
                    )
                    self.assertEqual(1, result.returncode, msg=result.stderr)
                    self.assertIn("native anchor retirement could not be confirmed", result.stderr)
                    self.assertIn("private Docker client state retained", result.stderr)
                    self.assertEqual(1, len(fixture.calls()), msg="A second native call overwrote unknown retirement")
                    self.assertEqual(before, (record.read_bytes(), record.stat().st_ino))
                    self.assert_owned_configuration(fixture, caller_paths, retired=False)
                    observed = observation.read_text().splitlines()
                    self.assertTrue((Path(observed[2]) / "command.log").is_file())
                    if action == "build":
                        capture = Path(observed[4])
                        self.assertTrue((capture / "context").is_dir())
                        self.assertTrue((capture / "wheelhouse").is_dir())
                        self.assertIn("private build capture retained", result.stderr)
                finally:
                    if observation.exists():
                        observed = observation.read_text().splitlines()
                        with suppress(ProcessLookupError):
                            os.killpg(int(observed[0]), signal.SIGKILL)
                        with suppress(ProcessLookupError):
                            os.kill(int(observed[1]), signal.SIGKILL)
                        for retained in observed[2:]:
                            if retained:
                                shutil.rmtree(retained)

    def test_interrupted_removal_finishes_remaining_cleanup_and_preserves_signal_status(self):
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            for effect in ("before", "after"):
                removal = (
                    '() { case "$*" in *nmrnet-docker-client.*) '
                    + ('command rm "$@"; ' if effect == "after" else "")
                    + f'builtin kill -{signum} "$BASHPID"; return 9;; *) command rm "$@";; esac; }}'
                )
                with self.subTest(signal=signum, effect=effect), tempfile.TemporaryDirectory() as directory:
                    fixture = LauncherFixture(Path(directory))
                    environment, caller_paths = self.caller_configuration(fixture.root)
                    result = fixture.run("inference_image.sh", "build", additions=environment | {"BASH_FUNC_rm%%": removal})
                    self.assertEqual(128 + signum, result.returncode, msg=result.stderr)
                    self.assertEqual("", result.stderr, msg="Pending cancellation must not start another reporting operation")
                    self.assertEqual({"image-id"}, {path.name for path in (fixture.root / ".work/inference").iterdir()})
                    path = self.assert_owned_configuration(fixture, caller_paths, retired=effect == "after")
                    if effect == "before":
                        path.rmdir()
                        path.parent.rmdir()

    def test_full_stderr_pipe_cancellation_follows_all_confirmed_cleanup(self):
        publication = '() { case "$*" in */image-id.*) return 11;; *) command mv "$@";; esac; }'
        for phase in ("removal", "reporting"):
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                with self.subTest(phase=phase, signal=signum), tempfile.TemporaryDirectory() as directory:
                    self.full_stderr_cleanup_case(Path(directory), phase=phase, signum=signum, publication=publication)

    def full_stderr_cleanup_case(self, directory, *, phase, signum, publication):
        removal = (
            '() { case "$*" in *nmrnet-docker-client.*) '
            'printf "%s\\n" "$docker_client_directory" "$capture_directory" > "$NMRNET_CLEANUP_OBSERVATION"; '
            'command rm "$@"; '
            + (f'builtin kill -{signum} "$BASHPID"; ' if phase == "removal" else "")
            + 'return 9;; *) command rm "$@";; esac; }'
        )
        fixture = LauncherFixture(Path(directory))
        observation = fixture.root / "cleanup-observation"
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
                ["bash", str(fixture.root / "tools/inference_image.sh"), "build"],
                env=fixture.environment
                | {
                    "BASH_FUNC_rm%%": removal,
                    "BASH_FUNC_mv%%": publication,
                    "NMRNET_CLEANUP_OBSERVATION": str(observation),
                },
                stdout=subprocess.PIPE,
                stderr=write_fd,
                start_new_session=True,
            )
            os.close(write_fd)
            write_fd = None
            reporting_processes = []
            if phase == "reporting":
                deadline = time.monotonic() + 3
                while not observation.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(observation.exists())
                blocked = False
                while not blocked and time.monotonic() < deadline:
                    pending = [process.pid]
                    reporting_processes = []
                    while pending:
                        identifier = pending.pop()
                        children = Path("/proc", str(identifier), "task", str(identifier), "children")
                        # Short-lived native control children can retire between
                        # procfs lookup and read; only an observed live writer
                        # admits the cancellation check below.
                        with suppress(FileNotFoundError, ProcessLookupError):
                            found = [int(value) for value in children.read_text().split()]
                            pending.extend(found)
                            reporting_processes.extend(found)
                    for identifier in reporting_processes:
                        channel = Path("/proc", str(identifier), "wchan")
                        with suppress(FileNotFoundError, ProcessLookupError):
                            if channel.read_text().strip() in {"pipe_write", "anon_pipe_write"}:
                                blocked = True
                    if not blocked:
                        time.sleep(0.01)
                self.assertTrue(blocked, msg="The native-owned cleanup reporter did not reach the full stderr pipe")
            else:
                # The only signal is sent by rm. No consumer drains stderr
                # and no second signal releases a report started too late.
                self.assertEqual(128 + signum, process.wait(timeout=3))
            self.assertTrue(observation.exists())
            for removed in observation.read_text().splitlines():
                self.assertFalse(Path(removed).exists(), msg="Diagnostics blocked before owned cleanup finished")
            self.assertEqual({"image-id"}, {path.name for path in (fixture.root / ".work/inference").iterdir()})
            if phase == "reporting":
                process.send_signal(signum)
            # Keep stderr unread until exit: draining it would conceal a
            # blocking diagnostic before removal or during cancellation.
            self.assertEqual(128 + signum, process.wait(timeout=2))
            deadline = time.monotonic() + 2
            while (
                any(Path("/proc", str(identifier)).exists() for identifier in reporting_processes) and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            self.assertFalse(any(Path("/proc", str(identifier)).exists() for identifier in reporting_processes))
        finally:
            if write_fd is not None:
                os.close(write_fd)
            os.close(read_fd)
            if process is not None:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=2)
