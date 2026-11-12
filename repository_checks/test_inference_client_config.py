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

    # silent-defaults: allow-contract -- Ordinary cleanup must leave the observed private configuration absent; explicit False instead requires retention; proof=repository_checks/test_inference_client_config.py::InferenceClientConfigTests.test_build_and_import_select_empty_owned_configuration_then_retire_it
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
                # silent-defaults: allow-contract -- Only the import expiry scenario supplies a short timeout; the build failure uses ordinary argv, and both must preserve their declared status and retire config; proof=repository_checks/test_inference_client_config.py::InferenceClientConfigTests.test_failed_build_and_expired_import_retire_owned_configuration
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
            # silent-defaults: allow-contract -- The after-removal stimulus really deletes before returning 9; the before-removal stimulus leaves it, and actual path effects and primary status are checked; proof=repository_checks/test_inference_client_config.py::InferenceClientConfigTests.test_cleanup_failure_keeps_primary_status_and_retained_configuration
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
                        # Successful preparation leaves its private mkdir diagnostic.
                        (path.parent / "preparation.log").unlink()
                        path.rmdir()
                        path.parent.rmdir()

    def test_unconfirmed_actual_native_retirement_stops_and_retains_owned_inputs(self):
        deny_retirement = (
            '() { if [[ "$1" == -KILL && "$operation" != "input capture" ]]; then '
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
                process = None
                try:
                    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                        process = subprocess.Popen(
                            ["bash", str(fixture.root / "tools/inference_image.sh"), action],
                            env=fixture.environment | environment | {
                                "BASH_FUNC_kill%%": deny_retirement,
                                "NMRNET_RETIREMENT_OBSERVATION": str(observation),
                            },
                            cwd=fixture.root, stdout=stdout, stderr=stderr, start_new_session=True,
                        )
                        self.assertEqual(1, process.wait(timeout=20))
                        stderr.seek(0)
                        diagnostic = stderr.read().decode()
                    self.assertIn("native anchor retirement could not be confirmed", diagnostic)
                    self.assertIn("private Docker client state retained", diagnostic)
                    calls = fixture.calls()
                    self.assertEqual(1, len(calls), msg="A second native call overwrote unknown retirement")
                    # silent-defaults: allow-contract -- Build inspects the pinned base image; import inspects the image recorded before the launch.
                    image = (
                        "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
                        if action == "build" else before[0].decode().strip()
                    )
                    self.assertEqual(["image", "inspect", "--format", "{{.Id}}", image], calls[0]["arguments"])
                    self.assertEqual(before, (record.read_bytes(), record.stat().st_ino))
                    self.assert_owned_configuration(fixture, caller_paths, retired=False)
                    observed = observation.read_text().splitlines()
                    self.assertTrue((Path(observed[2]) / "command.log").is_file())
                    if action == "build":
                        capture = Path(observed[4])
                        self.assertTrue((capture / "context").is_dir())
                        self.assertTrue((capture / "authored").is_file())
                        self.assertTrue((capture / "source").is_file())
                        self.assertIn("private build capture retained", diagnostic)
                finally:
                    if process is not None and process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                    if observation.exists():
                        observed = observation.read_text().splitlines()
                        with suppress(ProcessLookupError):
                            os.killpg(int(observed[0]), signal.SIGKILL)
                        if observed[1]:
                            with suppress(ProcessLookupError):
                                os.kill(int(observed[1]), signal.SIGKILL)
                        for retained in observed[2:]:
                            if retained:
                                shutil.rmtree(retained)

    def test_interrupted_removal_finishes_remaining_cleanup_and_preserves_signal_status(self):
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            for effect in ("before", "after"):
                # silent-defaults: allow-contract -- Only the after-removal stimulus deletes before injecting the real signal; both cases independently check path effects and signal status without a new report; proof=repository_checks/test_inference_client_config.py::InferenceClientConfigTests.test_interrupted_removal_finishes_remaining_cleanup_and_preserves_signal_status
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
                        (path.parent / "preparation.log").unlink()
                        path.rmdir()
                        path.parent.rmdir()

    def test_full_stderr_pipe_cancellation_follows_all_confirmed_cleanup(self):
        publication = '() { case "$*" in */image-id.*) return 11;; *) command mv "$@";; esac; }'
        for phase in ("removal", "reporting"):
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                with self.subTest(phase=phase, signal=signum), tempfile.TemporaryDirectory() as directory:
                    self.full_stderr_cleanup_case(Path(directory), phase=phase, signum=signum, publication=publication)

    def test_full_stdout_success_report_cancellation_retires_owner_before_cleanup(self):
        for action in ("build", "import"):
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                with self.subTest(action=action, signal=signum), tempfile.TemporaryDirectory() as directory:
                    self.full_stdout_success_case(Path(directory), action=action, signum=signum)

    def test_full_stderr_caller_failures_finish_cleanup_before_cancellable_reporting(self):
        for phase in ("validation", "mismatch"):
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                with self.subTest(phase=phase, signal=signum), tempfile.TemporaryDirectory() as directory:
                    self.full_stderr_caller_failure_case(Path(directory), phase=phase, signum=signum)

    def test_full_stderr_returned_native_failure_reports_after_cleanup_before_drain(self):
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as directory:
                self.full_stderr_caller_failure_case(Path(directory), phase="native", signum=signum)

    def test_full_stderr_capture_cancellation_retires_owned_work_before_drain(self):
        publication = '() { case "$*" in */image-id.*) return 11;; *) command mv "$@";; esac; }'
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as directory:
                self.full_stderr_cleanup_case(Path(directory), phase="capture", signum=signum, publication=publication)

    def full_stderr_caller_failure_case(self, directory, *, phase, signum):
        fixture = LauncherFixture(directory)
        environment, caller_paths = self.caller_configuration(fixture.root)
        observation = fixture.root / "caller-cleanup"
        native_receipt = fixture.root / "native-failure"
        removal = (
            '() { case "$*" in *nmrnet-inference-native.*/output*) '
            'printf "%s\\n" "${3%/output}" > "$NMRNET_NATIVE_FAILURE";; '
            "*nmrnet-docker-client.*) "
            'printf "%s\\n" "$docker_client_directory" "$capture_directory" > "$NMRNET_DIAGNOSTIC_CLEANUP";; '
            'esac; command rm "$@"; }'
        )
        additions = environment | {
            "BASH_FUNC_rm%%": removal,
            "NMRNET_DIAGNOSTIC_CLEANUP": str(observation),
            "NMRNET_NATIVE_FAILURE": str(native_receipt),
        }
        if phase == "validation":
            (fixture.root / "requirements/inference.lock").unlink()
            diagnostic = (
                "Cannot build inference image: requirement input must be a regular file without a symlink: inference.lock\n"
            )
        elif phase == "mismatch":
            additions["NMRNET_FAKE_BUILD_RESULT"] = "sha256:" + "d" * 64
            diagnostic = "Cannot record inference image: observed local image identity differs from the built image ID.\n"
        else:
            additions["NMRNET_FAKE_BUILD_STATUS"] = "7"
        record = fixture.root / ".work/inference/image-id"
        before = record.read_bytes(), record.stat().st_ino
        # Match the actual fixed report program as a process identity. The
        # assertions below independently require its cleanup/status effects.
        source = (fixture.root / "tools/inference_image.sh").read_text()
        payload = source.split("if inference_native 'cleanup reporting' untimed stdio bash -eu -c '", 1)[1].split("' bash ", 1)[0]
        read_fd, write_fd = os.pipe()
        process = None
        anchor = None
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
                env=fixture.environment | additions,
                stdout=subprocess.PIPE,
                stderr=write_fd,
                start_new_session=True,
            )
            os.close(write_fd)
            write_fd = None
            if phase == "native":
                phase_deadline = time.monotonic() + 30
                while (
                    not native_receipt.is_file()
                    and process.poll() is None
                    and time.monotonic() < phase_deadline
                ):
                    time.sleep(0.01)
                if not native_receipt.is_file():
                    self.fail(
                        f"{phase} failure control removal was not observed within 30 seconds; "
                        f"runner exit status: {process.poll()!r}"
                    )
                scratch = Path(native_receipt.read_text().strip())
                self.addCleanup(subprocess.run, ["/bin/rm", "-rf", "--", str(scratch)], check=True)
                self.assertEqual(("sha256:" + "b" * 64 + "\n").encode(), (scratch / "command.log").read_bytes())
                diagnostic = (
                    "Cannot complete build: native client exited with status 7; "
                    f"private capped output retained at {scratch / 'command.log'}.\n"
                )
            if phase == "mismatch":
                # The built-image inspection follows the offline build and its
                # wheel report. Wait for the owned config-removal receipt before
                # measuring the separate caller-reporting phase.
                phase_deadline = time.monotonic() + 30
                while not observation.is_file() and process.poll() is None and time.monotonic() < phase_deadline:
                    time.sleep(0.01)
                if not observation.is_file():
                    calls = [call["arguments"] for call in fixture.calls()]
                    self.fail(
                        "Inference image mismatch did not reach owned client cleanup within 30 seconds; "
                        f"runner exit status: {process.poll()!r}; expected diagnostic {diagnostic!r}; "
                        f"engine calls: {calls!r}"
                    )
            deadline = time.monotonic() + 3
            writer = None
            observed = []
            while writer is None and time.monotonic() < deadline:
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
                            writer = identifier
                if writer is None:
                    time.sleep(0.01)
            self.assertIsNotNone(writer, msg="The exact caller diagnostic reporter did not block on full stderr")
            anchor = os.getpgid(writer)
            self.assertIn(anchor, observed)
            self.assertNotEqual(writer, anchor)
            anchors = []
            for identifier in observed:
                with suppress(FileNotFoundError, ProcessLookupError):
                    command = Path("/proc", str(identifier), "cmdline").read_bytes()
                    self.assertNotIn(str(fixture.root / "bin/docker").encode(), command.split(b"\0"))
                    if b"receipt=$1 parent=$2 budget=$3 timed_program=$4" in command:
                        anchors.append(identifier)
            self.assertEqual([anchor], anchors)
            self.assertTrue(observation.is_file())
            removed = observation.read_text().splitlines()
            self.assertTrue(Path(removed[0]).name.startswith("nmrnet-docker-client."))
            for path in removed:
                if path:
                    self.assertFalse(Path(path).exists(), msg="Caller diagnostics preceded quiet owned cleanup")
            if phase in {"mismatch", "native"}:
                self.assertTrue(removed[1], msg="The failure case did not exercise a captured build")
            engine_calls = [call["arguments"] for call in fixture.calls()]
            if phase in {"mismatch", "native"}:
                expected_operations = ["image", "run", "run", "build"]
                if phase == "mismatch":
                    expected_operations.append("image")
                self.assertEqual(expected_operations, [call[0] for call in engine_calls])
                self.assertEqual(
                    ["image", "inspect", "--format", "{{.Id}}", "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"],
                    engine_calls[0],
                )
                lock_names = []
                for call in engine_calls[1:3]:
                    mount = next(
                        value for option, value in zip(call, call[1:])
                        if option == "--mount" and ",dst=/input/requirements.lock,readonly" in value
                    )
                    lock_names.append(Path(mount.split(",src=", 1)[1].split(",dst=", 1)[0]).name)
                self.assertEqual(["inference.lock", "provider-core-native-install.lock"], lock_names)
                dockerfile = engine_calls[3][engine_calls[3].index("--file") + 1]
                self.assertEqual("containers/inference/Dockerfile", dockerfile)
                if phase == "mismatch":
                    self.assertEqual(
                        ["image", "inspect", "--format", "{{.Id}}", "sha256:" + "d" * 64], engine_calls[4]
                    )
            else:
                self.assertEqual({"validation": 0, "native": 3}[phase], len(engine_calls))
            self.assertEqual(before, (record.read_bytes(), record.stat().st_ino))
            process.send_signal(signum)
            # Keep stderr unread until exit; the one signal must retire the
            # held publisher after the original failure's cleanup has finished.
            self.assertEqual(128 + signum, process.wait(timeout=2))
            self.assertFalse(Path("/proc", str(writer)).exists())
            self.assertFalse(Path("/proc", str(anchor)).exists())
            self.assertEqual(before, (record.read_bytes(), record.stat().st_ino))
            self.assertEqual({"image-id"}, {path.name for path in record.parent.iterdir()})
            self.assertEqual(engine_calls, [call["arguments"] for call in fixture.calls()])
            self.assertEqual(b"", process.stdout.read())
            for caller in caller_paths:
                self.assertTrue((caller / "config.json").is_file())
        finally:
            if write_fd is not None:
                os.close(write_fd)
            os.close(read_fd)
            if process is not None:
                if process.returncode is None:
                    # Hold the caller's unreaped identity through fallback.
                    # Closing stderr first lets its owner finish cooperatively.
                    with suppress(ProcessLookupError):
                        os.kill(process.pid, signal.SIGTERM)
                    deadline = time.monotonic() + 2
                    while (
                        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        deadline = time.monotonic() + 2
                        while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                            self.assertLess(time.monotonic(), deadline, msg="Failed caller termination was not observed")
                            time.sleep(0.01)
                    # Only an anchor admitted by this case's writer observation
                    # may be rescued; earlier failures authorize no group scan.
                    if anchor is not None:
                        with suppress(FileNotFoundError, ProcessLookupError):
                            arguments = Path("/proc", str(anchor), "cmdline").read_bytes().split(b"\0")
                            if (
                                os.getsid(anchor) == process.pid
                                and len(arguments) >= 6
                                and arguments[:2] == [b"bash", b"-c"]
                                and b"receipt=$1 parent=$2 budget=$3 timed_program=$4 deadline_signal=$5 grace=$6; shift 6"
                                in arguments[2]
                                and arguments[5] == str(process.pid).encode()
                            ):
                                os.killpg(anchor, signal.SIGKILL)
                                deadline = time.monotonic() + 2
                                while True:
                                    try:
                                        os.killpg(anchor, 0)
                                    except ProcessLookupError:
                                        break
                                    self.assertLess(time.monotonic(), deadline, msg="Observed reporter group did not retire")
                                    time.sleep(0.01)
                process.communicate(timeout=2)

    def full_stdout_success_case(self, directory, *, action, signum):
        fixture = LauncherFixture(directory)
        environment, caller_paths = self.caller_configuration(fixture.root)
        identity = "sha256:" + "d" * 64
        record = fixture.root / ".work/inference/image-id"
        before = record.read_bytes(), record.stat().st_ino
        payload = 'printf "Built inference image %s\\n" "$1"'
        if action == "import":
            payload = 'printf "%s\\n" "$1"'
        read_fd, write_fd = os.pipe()
        process = None
        observed = []
        try:
            os.set_blocking(write_fd, False)
            while True:
                try:
                    os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(write_fd, True)
            process = subprocess.Popen(
                ["bash", str(fixture.root / "tools/inference_image.sh"), action],
                env=fixture.environment | environment | {"NMRNET_FAKE_IMAGE_ID": identity, "NMRNET_FAKE_BUILD_RESULT": identity}
                if action == "build"
                else fixture.environment | environment,
                stdout=write_fd,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            os.close(write_fd)
            write_fd = None
            if action == "build":
                publication = (identity + "\n").encode()
                publication_deadline = time.monotonic() + 30
                while (
                    (record.read_bytes() != publication or record.stat().st_ino == before[1])
                    and process.poll() is None
                    and time.monotonic() < publication_deadline
                ):
                    time.sleep(0.01)
                if record.read_bytes() != publication or record.stat().st_ino == before[1]:
                    self.fail(
                        "Build did not publish the committed image identity within 30 seconds; "
                        f"caller status {process.poll()!r}; engine calls "
                        f"{[call['arguments'] for call in fixture.calls()]!r}"
                    )
            deadline = time.monotonic() + 3
            writer = None
            while writer is None and process.poll() is None and time.monotonic() < deadline:
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
                            writer = identifier
                if writer is None:
                    time.sleep(0.01)
            self.assertIsNotNone(writer, msg="The exact success reporter did not block on the full stdout pipe")
            anchor = os.getpgid(writer)
            self.assertIn(anchor, observed)
            self.assertNotEqual(writer, anchor)
            anchor_command = Path("/proc", str(anchor), "cmdline").read_bytes().split(b"\0")
            self.assertIn(b"untimed", anchor_command)
            self.assertIn(payload.encode(), anchor_command)
            # The only remaining native group belongs to this exact reporter;
            # previous captured clients and their drainers have already joined.
            anchors = []
            for identifier in observed:
                with suppress(FileNotFoundError, ProcessLookupError):
                    command = Path("/proc", str(identifier), "cmdline").read_bytes()
                    self.assertNotIn(str(fixture.root / "bin/docker").encode(), command.split(b"\0"))
                    self.assertNotIn(b"head\0-c\0" + b"8388608", command)
                    if b"receipt=$1 parent=$2 budget=$3 timed_program=$4" in command:
                        anchors.append(identifier)
            self.assertEqual([anchor], anchors, msg="The reporter must be the sole remaining native anchor")
            calls = fixture.calls()
            engine_calls = [call["arguments"] for call in calls]
            # silent-defaults: allow-contract -- Build and import scenarios expect seven and two recorded engine calls before the independently observed held success reporter; proof=repository_checks/test_inference_client_config.py::InferenceClientConfigTests.test_full_stdout_success_report_cancellation_retires_owner_before_cleanup
            expected_operations = (
                ["image", "run", "run", "build", "image", "image", "image"]
                if action == "build" else ["image", "run"]
            )
            self.assertEqual(expected_operations, [call[0] for call in engine_calls])
            configuration = Path(calls[0]["client_config"]["path"])
            self.assertTrue(configuration.is_dir())
            if action == "build":
                self.assertEqual((identity + "\n").encode(), record.read_bytes())
                self.assertNotEqual(before[1], record.stat().st_ino)
                self.assertEqual(1, len(list(record.parent.glob(".build.*"))))
                self.assertEqual(
                    [
                        "image", "inspect", "--format", "{{.Id}}",
                        "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c",
                    ],
                    engine_calls[0],
                )
                lock_names = []
                for call in engine_calls[1:3]:
                    mount = next(
                        value for option, value in zip(call, call[1:])
                        if option == "--mount" and ",dst=/input/requirements.lock,readonly" in value
                    )
                    lock_names.append(Path(mount.split(",src=", 1)[1].split(",dst=", 1)[0]).name)
                self.assertEqual(["inference.lock", "provider-core-native-install.lock"], lock_names)
                self.assertEqual("containers/inference/Dockerfile", engine_calls[3][engine_calls[3].index("--file") + 1])
                self.assertEqual(
                    ["image", "inspect", "--format", "{{.Id}}", identity], engine_calls[4]
                )
                for index, label in ((5, "inference-inputs"), (6, "inference-wheels")):
                    self.assertEqual(
                        [
                            "image", "inspect", "--format",
                            f'{{{{index .Config.Labels "io.nmrnet-repro.{label}"}}}}',
                            identity,
                        ],
                        engine_calls[index],
                    )
            else:
                self.assertEqual(before, (record.read_bytes(), record.stat().st_ino))
                self.assertEqual(
                    ["image", "inspect", "--format", "{{.Id}}", before[0].decode().strip()],
                    engine_calls[0],
                )
                self.assertEqual("run", engine_calls[1][0])
                self.assertEqual(before[0].decode().strip(), engine_calls[1][-1])
            process.send_signal(signum)
            # Do not drain stdout: the sole signal must retire the held writer
            # and let EXIT remove its configuration and captured build inputs.
            self.assertEqual(128 + signum, process.wait(timeout=2))
            self.assertFalse(Path("/proc", str(writer)).exists())
            self.assertFalse(Path("/proc", str(anchor)).exists())
            self.assert_owned_configuration(fixture, caller_paths)
            self.assertEqual({"image-id"}, {path.name for path in record.parent.iterdir()})
            self.assertEqual((identity + "\n").encode() if action == "build" else before[0], record.read_bytes())
            self.assertEqual(b"", process.stderr.read())
        finally:
            if write_fd is not None:
                os.close(write_fd)
            os.close(read_fd)
            if process is not None:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=2)

    def test_unconfirmed_success_report_retirement_retains_owned_inputs_and_committed_record(self):
        deny_retirement = (
            '() { if [[ "$1" == -KILL && "$operation" == *"success reporting" ]]; then '
            'printf "%s\\n" "$leader" "$scratch" "$docker_client_directory" "$capture_directory" '
            '> "$NMRNET_RETIREMENT_OBSERVATION"; return 1; fi; builtin kill "$@"; }'
        )
        for action in ("build", "import"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                environment, caller_paths = self.caller_configuration(fixture.root)
                observation = fixture.root / "report-retirement"
                record = fixture.root / ".work/inference/image-id"
                before = record.read_bytes(), record.stat().st_ino
                process = None
                try:
                    # Unknown stdio retirement can retain public writers. Wait
                    # for the launcher, without treating pipe EOF as its exit.
                    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                        process = subprocess.Popen(
                            ["bash", str(fixture.root / "tools/inference_image.sh"), action],
                            env=fixture.environment
                            | environment
                            | {"BASH_FUNC_kill%%": deny_retirement, "NMRNET_RETIREMENT_OBSERVATION": str(observation)},
                            stdout=stdout,
                            stderr=stderr,
                            start_new_session=True,
                        )
                        deadline = time.monotonic() + 30
                        while not observation.is_file() and process.poll() is None and time.monotonic() < deadline:
                            time.sleep(0.01)
                        if not observation.is_file():
                            calls = [call["arguments"] for call in fixture.calls()]
                            self.fail(
                                "Success-report retirement was not observed within 30 seconds; "
                                f"caller status {process.poll()!r}; engine calls {calls!r}"
                            )
                        self.assertEqual(1, process.wait(timeout=3))
                        stderr.seek(0)
                        diagnostic = stderr.read().decode()
                    self.assertIn("native anchor retirement could not be confirmed", diagnostic)
                    self.assertIn("private Docker client state retained", diagnostic)
                    engine_calls = [call["arguments"] for call in fixture.calls()]
                    # silent-defaults: allow-contract -- Build and import scenarios expect seven and two recorded engine calls; retained inputs and record effects separately check denied reporter retirement; proof=repository_checks/test_inference_client_config.py::InferenceClientConfigTests.test_unconfirmed_success_report_retirement_retains_owned_inputs_and_committed_record
                    expected_operations = (
                        ["image", "run", "run", "build", "image", "image", "image"]
                        if action == "build" else ["image", "run"]
                    )
                    self.assertEqual(expected_operations, [call[0] for call in engine_calls])
                    self.assert_owned_configuration(fixture, caller_paths, retired=False)
                    observed = observation.read_text().splitlines()
                    self.assertEqual(4, len(observed))
                    leader = int(observed[0])
                    scratch, client_directory, capture_directory = map(Path, observed[1:])
                    self.assertTrue(scratch.is_dir())
                    self.assertTrue(client_directory.is_dir())
                    arguments = Path("/proc", str(leader), "cmdline").read_bytes().split(b"\0")
                    self.assertEqual(process.pid, os.getsid(leader))
                    self.assertEqual(leader, os.getpgid(leader))
                    self.assertEqual([b"bash", b"-c"], arguments[:2])
                    self.assertIn(
                        b"receipt=$1 parent=$2 budget=$3 timed_program=$4 deadline_signal=$5 grace=$6; shift 6",
                        arguments[2],
                    )
                    self.assertEqual(str(process.pid).encode(), arguments[5])
                    if action == "build":
                        self.assertEqual(
                            [
                                "image", "inspect", "--format", "{{.Id}}",
                                "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c",
                            ],
                            engine_calls[0],
                        )
                        lock_names = []
                        for call in engine_calls[1:3]:
                            mount = next(
                                value for option, value in zip(call, call[1:])
                                if option == "--mount" and ",dst=/input/requirements.lock,readonly" in value
                            )
                            lock_names.append(Path(mount.split(",src=", 1)[1].split(",dst=", 1)[0]).name)
                        self.assertEqual(["inference.lock", "provider-core-native-install.lock"], lock_names)
                        dockerfile = engine_calls[3][engine_calls[3].index("--file") + 1]
                        self.assertEqual("containers/inference/Dockerfile", dockerfile)
                        self.assertEqual(
                            ["image", "inspect", "--format", "{{.Id}}", "sha256:" + "b" * 64], engine_calls[4]
                        )
                        self.assertEqual(
                            [
                                "image", "inspect", "--format",
                                '{{index .Config.Labels "io.nmrnet-repro.inference-inputs"}}',
                                "sha256:" + "b" * 64,
                            ],
                            engine_calls[5],
                        )
                        self.assertEqual(
                            [
                                "image", "inspect", "--format",
                                '{{index .Config.Labels "io.nmrnet-repro.inference-wheels"}}',
                                "sha256:" + "b" * 64,
                            ],
                            engine_calls[6],
                        )
                        self.assertNotEqual(before[1], record.stat().st_ino)
                        self.assertTrue((capture_directory / "context").is_dir())
                        for inventory in (
                            "authored",
                            "source",
                            "sources.json",
                            "manifest.json",
                            "science.report.json",
                            "science.manifest.json",
                            "runtime.report.json",
                            "runtime.manifest.json",
                        ):
                            self.assertTrue((capture_directory / inventory).is_file(), msg=inventory)
                        self.assertIn("private build capture retained", diagnostic)
                    else:
                        self.assertEqual(
                            ["image", "inspect", "--format", "{{.Id}}", before[0].decode().strip()],
                            engine_calls[0],
                        )
                        self.assertEqual("run", engine_calls[1][0])
                        self.assertEqual(before[0].decode().strip(), engine_calls[1][-1])
                        self.assertEqual(before, (record.read_bytes(), record.stat().st_ino))
                    self.assertEqual(before[0], record.read_bytes())
                finally:
                    if process is not None and process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                    if observation.exists():
                        observed = observation.read_text().splitlines()
                        with suppress(ProcessLookupError):
                            os.killpg(int(observed[0]), signal.SIGKILL)
                        for retained in observed[1:]:
                            if retained:
                                shutil.rmtree(retained)

    def full_stderr_cleanup_case(self, directory, *, phase, signum, publication):
        environment, caller_paths = self.caller_configuration(Path(directory))
        removal = (
            '() { case "$*" in *nmrnet-docker-client.*) '
            'printf "%s\\n" "$docker_client_directory" "$capture_directory" > "$NMRNET_CLEANUP_OBSERVATION"; '
            'command rm "$@"; '
            + (f'builtin kill -{signum} "$BASHPID"; ' if phase == "removal" else "")
            + ("" if phase == "capture" else "return 9")
            + ';; *) command rm "$@";; esac; }'
        )
        chmod = (
            '() { case "$3" in */context/*) head -c 64 /dev/zero >&2; return 37;; '
            'esac; command chmod "$@"; }'
        )
        fixture = LauncherFixture(Path(directory))
        observation = fixture.root / "cleanup-observation"
        read_fd, write_fd = os.pipe()
        process = None
        observed_capture_group = None
        try:
            os.set_blocking(write_fd, False)
            while True:
                try:
                    os.write(write_fd, b"x" * 4096)
                except BlockingIOError:
                    break
            os.set_blocking(write_fd, True)
            # silent-defaults: allow-contract -- Only capture injects the chmod writer used to hold stderr.
            process = subprocess.Popen(
                ["bash", str(fixture.root / "tools/inference_image.sh"), "build"],
                env=fixture.environment
                | environment
                | {
                    "BASH_FUNC_rm%%": removal,
                    "BASH_FUNC_mv%%": publication,
                    "NMRNET_CLEANUP_OBSERVATION": str(observation),
                    **({"BASH_FUNC_chmod%%": chmod} if phase == "capture" else {}),
                },
                stdout=subprocess.PIPE,
                stderr=write_fd,
                start_new_session=True,
            )
            os.close(write_fd)
            write_fd = None
            reporting_processes = []
            if phase != "capture":
                deadline = time.monotonic() + 30
                while not observation.is_file() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(
                    observation.is_file(),
                    msg=f"Build did not reach owned cleanup; caller status {process.poll()!r}",
                )
            if phase in {"reporting", "capture"}:
                # silent-defaults: allow-contract -- Capture's wait includes context setup; reporting starts its short wait after owned cleanup.
                deadline = time.monotonic() + (30 if phase == "capture" else 3)
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
                            is_capture_writer = False
                            if phase == "capture":
                                command = Path("/proc", str(identifier), "cmdline").read_bytes().split(b"\0")
                                is_capture_writer = command[:4] == [b"head", b"-c", b"64", b"/dev/zero"]
                            if channel.read_text().strip() in {"pipe_write", "anon_pipe_write"} and (
                                phase == "reporting" or is_capture_writer
                            ):
                                blocked = True
                                if phase == "capture":
                                    writer = identifier
                    if not blocked:
                        time.sleep(0.01)
                # silent-defaults: allow-contract -- The failure message names the writer selected by the authored phase.
                self.assertTrue(
                    blocked,
                    msg=(
                        "The injected context chmod did not block its marker writer on stderr"
                        if phase == "capture"
                        else "The native-owned cleanup reporter did not reach the full stderr pipe"
                    ),
                )
                if phase == "capture":
                    anchor = os.getpgid(writer)
                    self.assertEqual(process.pid, os.getsid(anchor))
                    self.assertEqual(anchor, os.getpgid(anchor))
                    anchors = []
                    source = (fixture.root / "tools/inference_image.sh").read_text()
                    capture_payload = source.split(
                        "inference_native 'input capture' untimed stdio bash -eu -c '", 1
                    )[1].split("' bash ", 1)[0]
                    capture_workers = []
                    for identifier in reporting_processes:
                        with suppress(FileNotFoundError, ProcessLookupError):
                            arguments = Path("/proc", str(identifier), "cmdline").read_bytes().split(b"\0")
                            if (
                                len(arguments) >= 6
                                and arguments[:2] == [b"bash", b"-c"]
                                and b"receipt=$1 parent=$2 budget=$3 timed_program=$4 deadline_signal=$5 grace=$6; shift 6"
                                in arguments[2]
                                and arguments[5] == str(process.pid).encode()
                            ):
                                anchors.append(identifier)
                            if arguments[1:4] == [b"-eu", b"-c", capture_payload.encode()]:
                                capture_workers.append((identifier, arguments))
                    self.assertEqual([anchor], anchors, msg="The blocked chmod writer had no unique native owner")
                    self.assertEqual(1, len(capture_workers), msg="The blocked writer had no unique input-capture command")
                    capture_worker, worker_arguments = capture_workers[0]
                    self.assertEqual(anchor, os.getpgid(capture_worker))
                    self.assertEqual([b"bash", str(fixture.root).encode()], worker_arguments[4:6])
                    self.assertEqual(
                        (fixture.root / ".work/inference").as_posix().encode(),
                        Path(worker_arguments[6].decode()).parent.as_posix().encode(),
                    )
                    owned = []
                    for identifier in reporting_processes:
                        with suppress(ProcessLookupError):
                            if os.getpgid(identifier) == anchor:
                                owned.append(identifier)
                    self.assertIn(writer, owned)
                    self.assertEqual([], fixture.calls(), msg="Input capture reached an engine effect before admission")
                    observed_capture_group = anchor
            else:
                # The only signal is sent by rm. No consumer drains stderr
                # and no second signal releases a report started too late.
                self.assertEqual(128 + signum, process.wait(timeout=3))
            if phase != "capture":
                self.assertTrue(observation.exists())
                for removed in observation.read_text().splitlines():
                    self.assertFalse(Path(removed).exists(), msg="Diagnostics blocked before owned cleanup finished")
                self.assertEqual({"image-id"}, {path.name for path in (fixture.root / ".work/inference").iterdir()})
            if phase in {"reporting", "capture"}:
                process.send_signal(signum)
            # Keep stderr unread until exit: draining it would conceal a
            # blocking diagnostic before removal or during cancellation.
            # silent-defaults: allow-contract -- Removal and reporting use the shorter check; capture waits longer for its held writer to retire.
            self.assertEqual(128 + signum, process.wait(timeout=4 if phase == "capture" else 2))
            if phase == "capture":
                self.assertTrue(observation.is_file(), msg="Caller cleanup did not observe its owned paths")
                removed_paths = observation.read_text().splitlines()
                self.assertEqual(2, len(removed_paths))
                self.assertTrue(all(removed_paths), msg="Caller cleanup receipt omitted an owned path")
                self.assertEqual(removed_paths[1], worker_arguments[6].decode())
                for removed in removed_paths:
                    self.assertFalse(Path(removed).exists(), msg="Cancellation returned before owned cleanup finished")
                self.assertEqual({"image-id"}, {path.name for path in (fixture.root / ".work/inference").iterdir()})
                self.assertFalse(any(Path("/proc", str(identifier)).exists() for identifier in owned))
                self.assertEqual([], fixture.calls())
                for caller in caller_paths:
                    self.assertTrue((caller / "config.json").is_file())
            deadline = time.monotonic() + 2
            retired_processes = owned if phase == "capture" else reporting_processes
            while any(Path("/proc", str(identifier)).exists() for identifier in retired_processes) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(any(Path("/proc", str(identifier)).exists() for identifier in retired_processes))
        finally:
            if write_fd is not None:
                os.close(write_fd)
            os.close(read_fd)
            if process is not None:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    if observed_capture_group is not None:
                        with suppress(FileNotFoundError, ProcessLookupError):
                            if os.getsid(observed_capture_group) == process.pid and os.getpgid(observed_capture_group) == observed_capture_group:
                                os.killpg(observed_capture_group, signal.SIGKILL)
                                deadline = time.monotonic() + 2
                                while True:
                                    try:
                                        os.killpg(observed_capture_group, 0)
                                    except ProcessLookupError:
                                        break
                                    self.assertLess(time.monotonic(), deadline, msg="Observed capture owner survived rescue")
                                    time.sleep(0.01)
                process.communicate(timeout=2)
