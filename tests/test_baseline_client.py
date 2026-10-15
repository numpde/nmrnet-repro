"""Exercise baseline-owned client state and native streams with finite engines."""

from tools.container_guard import require_container

require_container()

import json
import os
import select
import signal
import subprocess
import time
import unittest
from contextlib import suppress
from pathlib import Path

from tests import test_baseline_admission as admission_fixture


class BaselineClientTests(unittest.TestCase):
    setUp = admission_fixture.BaselineAdmissionTests.setUp
    command = admission_fixture.BaselineAdmissionTests.command
    engine_calls = admission_fixture.BaselineAdmissionTests.engine_calls
    retain_cleanup = admission_fixture.BaselineAdmissionTests.retain_cleanup
    assert_no_run = admission_fixture.BaselineAdmissionTests.assert_no_run

    def await_file(self, path):
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), msg=f"fixture did not publish {path.name}")

    def retire_fixture(self, identifier):
        with suppress(ProcessLookupError):
            os.kill(identifier, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while Path("/proc", str(identifier)).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(Path("/proc", str(identifier)).exists())

    def test_owned_same_empty_config_and_scrubbed_environment_both_phases(self):
        names = (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "DOCKER_CONFIG",
            "DOCKER_AUTH_CONFIG",
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
        command, environment = self.command(
            "run", dict.fromkeys(names, "poison") | {"DOCKER_BUILDKIT": "0", "BASELINE_SCRUB_NAMES": ",".join(names)}
        )
        result = subprocess.run(command, env=environment, capture_output=True, timeout=5, check=False)
        self.assertEqual(0, result.returncode, msg=result.stderr.decode())
        calls = self.engine_calls()
        self.assertEqual(2, len(calls))
        self.assertEqual(calls[0]["config"], calls[1]["config"])
        self.assertFalse(Path(calls[0]["config"]).parent.exists())

    def test_native_status_and_config_cleanup_failure_preserve_effect_truth(self):
        for status, removal, expected in (("0", "after-delete", 23), ("7", "after-delete", 7), ("137", "after-delete", 137)):
            with self.subTest(status=status):
                # Each matrix case owns a complete fixture. Metadata inherits
                # its 4-KiB limit; an earlier scientific argv can fill its log.
                self.setUp()
                command, environment = self.command(
                    "run",
                    {
                        "BASELINE_RUN_BEHAVIOR": "finite",
                        "BASELINE_RUN_STATUS": status,
                        "BASELINE_RUN_OBSERVATION": str(self.root / "scientific"),
                        "BASELINE_CONFIG_REMOVE": removal,
                    },
                )
                result = subprocess.run(command, env=environment, capture_output=True, timeout=5, check=False)
                self.assertEqual(expected, result.returncode, msg=result.stderr.decode())
                self.assertEqual(b"stdout\0\r\n", result.stdout)
                self.assertTrue(result.stderr.startswith(b"stderr\0\r\n"))
                self.assertIn(b"attempted path", result.stderr)
                self.assertNotIn(b"retained", result.stderr)
                self.assertFalse(Path(self.engine_calls()[-1]["config"]).parent.exists())

    def test_live_native_binary_streams_keep_config_until_retirement(self):
        observation = self.root / "scientific"
        command, environment = self.command(
            "run", {"BASELINE_RUN_BEHAVIOR": "live", "BASELINE_RUN_STATUS": "0", "BASELINE_RUN_OBSERVATION": str(observation)}
        )
        process = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        self.await_file(observation)
        observed = json.loads(observation.read_text())
        try:
            for pipe, expected in ((process.stdout, b"stdout\0\r\n"), (process.stderr, b"stderr\0\r\n")):
                self.assertEqual([pipe], select.select([pipe], [], [], 2)[0])
                self.assertEqual(expected, os.read(pipe.fileno(), len(expected)))
            self.assertTrue(Path(observed["config"]).is_dir())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(143, process.wait(timeout=4))
            self.assertFalse(Path(observed["config"]).parent.exists())
        finally:
            self.retire_fixture(observed["pid"])
            self.retire_fixture(observed["descendant"])
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=4)

    def test_unread_native_output_cancellation_removes_config_before_drain(self):
        for stream in ("stdout", "stderr"):
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                with self.subTest(stream=stream, signal=signum):
                    self.setUp()
                    observation = self.root / "scientific"
                    command, environment = self.command(
                        "run",
                        {
                            "BASELINE_RUN_BEHAVIOR": "blocked",
                            "BASELINE_RUN_STATUS": "0",
                            "BASELINE_BLOCK_STREAM": stream,
                            "BASELINE_RUN_OBSERVATION": str(observation),
                        },
                    )
                    process = subprocess.Popen(
                        command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                    )
                    self.addCleanup(admission_fixture.BaselineAdmissionTests.retire_probe, self, process)
                    self.await_file(observation)
                    observed = json.loads(observation.read_text())
                    try:
                        channel = Path("/proc", str(observed["pid"]), "wchan")
                        deadline = time.monotonic() + 2
                        while (
                            channel.read_text().strip() not in {"pipe_write", "anon_pipe_write"} and time.monotonic() < deadline
                        ):
                            time.sleep(0.01)
                        self.assertIn(channel.read_text().strip(), {"pipe_write", "anon_pipe_write"})
                        self.assertTrue(Path(observed["config"]).is_dir())
                        process.send_signal(signum)
                        self.assertEqual(128 + signum, process.wait(timeout=4))
                        self.assertFalse(Path(observed["config"]).parent.exists())
                    finally:
                        self.retire_fixture(observed["pid"])
                        self.retire_fixture(observed["descendant"])
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                        process.communicate(timeout=4)

    def test_unknown_inspect_anchor_retains_config_and_stops_before_run(self):
        helper = self.root / "tools/inference_native.sh"
        source = helper.read_text()
        capture = "        drain=$!\n"
        self.assertEqual(1, source.count(capture))
        receipt = self.root / "drain"
        helper.write_text(source.replace(capture, capture + '        printf "%s\\n" "$drain" > "$BASELINE_DRAIN"\n', 1))
        command, environment = self.command("run", {"BASELINE_HOLD": "1", "BASELINE_DRAIN": str(receipt)})
        process = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        self.await_file(self.observation)
        observed = json.loads(self.observation.read_text())
        config = Path(self.engine_calls()[0]["config"])
        os.kill(observed["anchor"], signal.SIGKILL)
        try:
            _, stderr = process.communicate(timeout=4)
            self.retain_cleanup(stderr.decode())
            self.assertEqual(1, process.returncode, msg=stderr.decode())
            self.assertTrue(config.is_dir())
            self.assertIn(b"client state retained", stderr)
            self.assert_no_run()
        finally:
            self.retire_fixture(observed["pid"])
            self.retire_fixture(int(receipt.read_text()))

    def test_unknown_scientific_anchor_retains_config_with_live_pipe_writers(self):
        observation = self.root / "scientific"
        command, environment = self.command(
            "run", {"BASELINE_RUN_BEHAVIOR": "live", "BASELINE_RUN_STATUS": "7", "BASELINE_RUN_OBSERVATION": str(observation)}
        )
        process = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        self.addCleanup(admission_fixture.BaselineAdmissionTests.retire_probe, self, process)
        self.await_file(observation)
        observed = json.loads(observation.read_text())
        config = Path(observed["config"])
        sentinel = config / "caller-state"
        sentinel.write_bytes(b"owned state")
        try:
            os.kill(observed["anchor"], signal.SIGKILL)
            # Exit is not EOF: surviving recorded writers still hold the
            # public streams. Observe status before retiring those exact PIDs.
            self.assertEqual(1, process.wait(timeout=4))
            self.assertEqual(b"owned state", sentinel.read_bytes())
            self.assertTrue(Path("/proc", str(observed["pid"])).exists())
            self.assertTrue(Path("/proc", str(observed["descendant"])).exists())
            self.assertEqual(2, len(self.engine_calls()))
        finally:
            self.retire_fixture(observed["pid"])
            self.retire_fixture(observed["descendant"])
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            _, stderr = process.communicate(timeout=4)
            self.retain_cleanup(stderr.decode())

    def test_poison_cleanup_path_survives_validation_and_print_exits(self):
        sentinel = self.root / "ambient"
        sentinel.mkdir()
        for arguments in (("unknown",), ("--print", "run")):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    ["bash", str(self.root / "tools/baseline.sh"), *arguments],
                    env=self.environment | {"docker_client_directory": str(sentinel)},
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertTrue(sentinel.is_dir())
                self.assertEqual([], self.engine_calls())
                if arguments[0] == "--print":
                    self.assertEqual(0, result.returncode)
                    self.assertFalse((self.root / ".work/baseline/runs").exists())

    def test_full_unread_mismatch_reports_after_cleanup_and_cancels_before_drain(self):
        payload = 'printf "%s" "$1" >&2'
        diagnostic = (
            "Cannot run baseline: observed inference image identity or upstream revision differs from the recorded inputs.\n"
        )
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.setUp()
                record = self.root / ".work/inference/image-id"
                before = record.read_bytes(), record.stat().st_ino
                command, environment = self.command("run", {"BASELINE_MUTATION": "wrong-id"})
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
                        command, env=environment, stdout=subprocess.PIPE, stderr=write_fd, start_new_session=True
                    )
                    os.close(write_fd)
                    write_fd = None
                    self.await_file(self.observation)
                    receipt = json.loads(self.observation.read_text())
                    directory = Path(receipt["directory"])
                    self.addCleanup(subprocess.run, ["/bin/rm", "-rf", "--", str(directory)], check=True)
                    expected = (
                        diagnostic + f"Baseline image admission did not complete; private receipts retained at {directory}.\n"
                    )
                    writer = None
                    observed = []
                    deadline = time.monotonic() + 3
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
                                arguments = Path("/proc", str(identifier), "cmdline").read_bytes().split(b"\0")
                                channel = Path("/proc", str(identifier), "wchan").read_text().strip()
                                if arguments[1:4] == [b"-eu", b"-c", payload.encode()] and channel in {
                                    "pipe_write",
                                    "anon_pipe_write",
                                }:
                                    self.assertEqual(expected.encode(), arguments[5])
                                    writer = identifier
                        if writer is None:
                            time.sleep(0.01)
                    self.assertIsNotNone(writer, msg="Actual baseline rejection publisher pipe-write was not observed")
                    anchor = os.getpgid(writer)
                    self.assertIn(anchor, observed)
                    self.assertNotEqual(writer, anchor)
                    anchors = []
                    for identifier in observed:
                        with suppress(FileNotFoundError, ProcessLookupError):
                            arguments = Path("/proc", str(identifier), "cmdline").read_bytes().split(b"\0")
                            self.assertNotIn(str(self.binary / "docker").encode(), arguments)
                            if b"receipt=$1 parent=$2 budget=$3 timed_program=$4" in b"\0".join(arguments):
                                anchors.append(identifier)
                    self.assertEqual([anchor], anchors)
                    self.assertFalse(Path("/proc", str(receipt["pid"])).exists())
                    self.assertFalse(Path("/proc", str(receipt["anchor"])).exists())
                    calls = self.engine_calls()
                    self.assertEqual(1, len(calls))
                    self.assertFalse(Path(calls[0]["config"]).parent.exists())
                    self.assertTrue(directory.is_dir())
                    self.assertTrue((directory / "stdout").is_file())
                    self.assertTrue((directory / "stderr").is_file())
                    self.assert_no_run()
                    self.assertEqual(before, (record.read_bytes(), record.stat().st_ino))
                    process.send_signal(signum)
                    # Observe exit before draining the actual full stderr pipe.
                    self.assertEqual(128 + signum, process.wait(timeout=4))
                    self.assertFalse(Path("/proc", str(writer)).exists())
                    self.assertFalse(Path("/proc", str(anchor)).exists())
                    self.assertEqual(b"", process.stdout.read())
                    self.assertEqual(before, (record.read_bytes(), record.stat().st_ino))
                finally:
                    if write_fd is not None:
                        os.close(write_fd)
                    os.close(read_fd)
                    if process is not None:
                        if process.returncode is None:
                            # Keep the caller unreaped through bounded rescue;
                            # only this case's observed anchor can be admitted.
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
                            if anchor is not None:
                                with suppress(FileNotFoundError, ProcessLookupError):
                                    arguments = Path("/proc", str(anchor), "cmdline").read_bytes().split(b"\0")
                                    if (
                                        os.getsid(anchor) == process.pid
                                        and len(arguments) >= 6
                                        and arguments[:2] == [b"bash", b"-c"]
                                        and b"receipt=$1 parent=$2 budget=$3 timed_program=$4; shift 4" in arguments[2]
                                        and arguments[5] == str(process.pid).encode()
                                    ):
                                        os.killpg(anchor, signal.SIGKILL)
                                        deadline = time.monotonic() + 2
                                        while True:
                                            try:
                                                os.killpg(anchor, 0)
                                            except ProcessLookupError:
                                                break
                                            self.assertLess(time.monotonic(), deadline, msg="Observed reporter did not retire")
                                            time.sleep(0.01)
                        process.communicate(timeout=2)

    def test_actual_native_cancellation_skips_cleanup_report_but_native_130_reports(self):
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.setUp()
                observation = self.root / "scientific"
                command, environment = self.command(
                    "run",
                    {
                        "BASELINE_RUN_BEHAVIOR": "blocked",
                        "BASELINE_RUN_STATUS": "0",
                        "BASELINE_BLOCK_STREAM": "stderr",
                        "BASELINE_RUN_OBSERVATION": str(observation),
                        "BASELINE_CONFIG_REMOVE": "after-delete",
                    },
                )
                process = subprocess.Popen(
                    command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                )
                self.await_file(observation)
                observed = json.loads(observation.read_text())
                try:
                    channel = Path("/proc", str(observed["pid"]), "wchan")
                    deadline = time.monotonic() + 2
                    while channel.read_text().strip() not in {"pipe_write", "anon_pipe_write"} and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertIn(channel.read_text().strip(), {"pipe_write", "anon_pipe_write"})
                    self.assertTrue(Path(observed["config"]).is_dir())
                    process.send_signal(signum)
                    # A second cleanup publisher would block on this same pipe
                    # and require another signal. Actual cancellation skips it.
                    self.assertEqual(128 + signum, process.wait(timeout=4))
                    self.assertFalse(Path(observed["config"]).parent.exists())
                    self.assertFalse(Path("/proc", str(observed["anchor"])).exists())
                    _, stderr = process.communicate(timeout=2)
                    self.assertNotIn(b"Cannot confirm baseline Docker", stderr)
                finally:
                    self.retire_fixture(observed["pid"])
                    self.retire_fixture(observed["descendant"])
                    if process.poll() is None:
                        process.send_signal(signal.SIGTERM)
                    process.communicate(timeout=4)
        self.setUp()
        command, environment = self.command(
            "run",
            {
                "BASELINE_RUN_BEHAVIOR": "finite",
                "BASELINE_RUN_STATUS": "130",
                "BASELINE_RUN_OBSERVATION": str(self.root / "scientific"),
                "BASELINE_CONFIG_REMOVE": "after-delete",
            },
        )
        result = subprocess.run(command, env=environment, capture_output=True, timeout=5, check=False)
        self.assertEqual(130, result.returncode, msg=result.stderr.decode())
        self.assertIn(b"configuration removal; attempted path", result.stderr)
        self.assertIn(b"(status 23)", result.stderr)
        self.assertFalse(Path(self.engine_calls()[-1]["config"]).parent.exists())
