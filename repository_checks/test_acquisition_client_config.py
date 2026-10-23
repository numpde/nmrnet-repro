#!/usr/bin/env python3
"""Observe acquisition CLI ownership with finite clients inside the checker.

The actual shared native helper owns local lifetime. These cases exercise raw
CID/stdout admission, private client state and public pipe cancellation without
accessing an engine, starting a real proxy or transferring scientific inputs.
"""

from tools.container_guard import require_container

require_container()

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path

from repository_checks.fixtures.containers.fixture import LauncherFixture, read_engine_calls
from repository_checks.quality.sources import ROOT


class AcquisitionClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = LauncherFixture(self.root)
        self.addCleanup(self.fixture.dispose)
        self.retained = set()
        self.addCleanup(self._remove_retained)
        self.observed = {}
        self.environment = self.fixture.environment | {
            "NMRNET_WLAN_INTERFACE_INPUT": "lo",
            "NMRNET_ACQUISITION_FIXTURE_ROOT": str(self.root),
        }
        image = "docker.io/library/python"
        version = "3.12.12-alpine3.22"
        sha = "848ba4413eb897e225159b8fc1b02094576cbae4aa73fc13142608ae2c8c0e32"
        self.proxy_image = f"{image}:{version}@sha256:{sha}"
        worker_version = "3.12.12-slim-bookworm"
        worker_sha = "593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
        self.worker_image = f"{image}:{worker_version}@sha256:{worker_sha}"

    def _intercept(self, *names):
        for name in names:
            (self.root / "bin" / name).symlink_to(Path(__file__).resolve())

    def _remove_retained(self):
        for path in self.retained:
            subprocess.run(["/bin/rm", "-rf", "--", str(path)], check=True)

    def _remember(self, stderr=""):
        for path in re.findall(r"/tmp/nmrnet-(?:docker-client|inference-native|proxy)\.[A-Za-z0-9]+", stderr):
            self.retained.add(Path(path))
        for call in self.fixture.calls():
            if call["client_config"] is not None:
                self.retained.add(Path(call["client_config"]["path"]).parent)
            if call["arguments"][:1] == ["create"]:
                values = call["arguments"]
                self.retained.add(Path(values[values.index("--cidfile") + 1]).parent)

    def caller(self, *arguments, additions=None):
        # silent-defaults: allow-contract -- Omitted additions supply no per-call overrides; supplied entries override the fixture environment; proof=repository_checks/test_acquisition_client_config.py::AcquisitionClientTests.test_every_engine_phase_uses_private_client_and_preserves_live_worker_bytes
        result = self.fixture.run("docker.sh", *arguments, additions=self.environment | (additions or {}))
        self._remember(result.stderr)
        return result

    def _anchor(self, group, parent):
        try:
            arguments = Path("/proc", str(group), "cmdline").read_bytes().split(b"\0")
            if (
                os.getsid(group) == parent
                and len(arguments) >= 6
                and arguments[:2] == [b"bash", b"-c"]
                and b"receipt=$1 parent=$2 budget=$3 timed_program=$4 deadline_signal=$5 grace=$6; shift 6" in arguments[2]
                and arguments[5] == str(parent).encode()
            ):
                return arguments
        except (FileNotFoundError, ProcessLookupError):
            return None
        return None

    def _observe_client(self, process, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), msg="actual finite client did not publish its phase")
        identifier = int(path.read_text().splitlines()[-1])
        group = os.getpgid(identifier)
        self.assertIsNotNone(self._anchor(group, process.pid))
        # silent-defaults: allow-contract -- Initialize a caller registry only after its live native anchor matches; rescue rechecks each recorded anchor before admitting it; proof=repository_checks/test_acquisition_client_config.py::AcquisitionClientTests.test_actual_attach_interrupt_finishes_without_draining_full_stderr
        self.observed.setdefault(process.pid, set()).add(group)
        return identifier

    def _rescue(self, process):
        if process.returncode is not None:
            return
        # Preserve the unreaped caller identity while positively observing its
        # death, then admit ONLY anchors already observed for this exact caller.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        deadline = time.monotonic() + 3
        while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            self.assertLess(time.monotonic(), deadline, msg="failed-probe caller death was not observed")
            time.sleep(0.01)
        admitted = set()
        paths = set()
        # silent-defaults: allow-contract -- An absent caller entry supplies no previously observed anchors to admit; each recorded anchor is rechecked before rescue; proof=repository_checks/test_acquisition_client_config.py::AcquisitionClientTests.test_actual_attach_interrupt_finishes_without_draining_full_stderr
        for group in self.observed.get(process.pid, set()):
            arguments = self._anchor(group, process.pid)
            if arguments is not None:
                with suppress(ProcessLookupError):
                    os.killpg(group, signal.SIGKILL)
                    admitted.add(group)
                    paths.add(Path(arguments[4].decode()).parent)
        for group in admitted:
            while True:
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    break
                self.assertLess(time.monotonic(), deadline, msg="failed-probe admitted group did not retire")
                time.sleep(0.01)
        process.communicate(timeout=3)
        self.retained.update(paths)
        self._remember()

    # silent-defaults: allow-contract -- Ordinary probes capture real stderr; full_stderr explicitly selects the unread-full-pipe stimulus; proof=repository_checks/test_acquisition_client_config.py::AcquisitionClientTests.test_actual_create_cancel_never_erases_disagreeing_or_nul_stdout
    def _start(self, additions, *, full_stderr=False):
        read_descriptor = None
        prefill = b""
        stderr = subprocess.PIPE
        if full_stderr:
            read_descriptor, stderr = os.pipe()
            self.addCleanup(os.close, read_descriptor)
            os.set_blocking(stderr, False)
            while True:
                try:
                    count = os.write(stderr, b"z" * 4096)
                    prefill += b"z" * count
                except BlockingIOError:
                    break
            os.set_blocking(stderr, True)
        process = subprocess.Popen(
            ["bash", str(self.root / "tools/docker.sh"), "run", "sha256:" + "b" * 64],
            env=self.environment | additions,
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=stderr,
            start_new_session=True,
        )
        if full_stderr:
            os.close(stderr)
        self.addCleanup(self._rescue, process)
        return process, read_descriptor, prefill

    def _finish_full_pipe(self, process, descriptor, prefill, interrupt, status):
        process.send_signal(interrupt)
        # No byte is drained before the sole signal has completed the caller.
        self.assertEqual(status, process.wait(timeout=5))
        stdout, _ = process.communicate(timeout=3)
        self.assertEqual(b"", stdout)
        os.set_blocking(descriptor, False)
        drained = b""
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            drained += chunk
        self.assertEqual(prefill, drained, msg="actual cancellation started another public reporter")
        self._remember()

    def _wait_report(self, process, scratch):
        source = (self.root / "tools/docker.sh").read_text()
        start = "if inference_native 'acquisition cleanup reporting' untimed stdio bash -eu -c '"
        self.assertEqual(1, source.count(start))
        program = source.split(start)[1].split("' bash ", 1)[0].encode()
        pending = (
            f"Cannot confirm acquisition worker removal: its container ID was not admitted; evidence remains in {scratch}.\n"
        )
        expected = [b"bash", b"-eu", b"-c", program, b"bash", pending.encode(), b"", b"0", str(scratch).encode(), b"0", b""]
        return self._observe_reporter(process, expected)

    def _observe_reporter(self, process, expected):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            children = Path("/proc", str(process.pid), "task", str(process.pid), "children").read_text().split()
            for child in children:
                group = int(child)
                if self._anchor(group, process.pid) is None:
                    continue
                try:
                    publishers = Path("/proc", child, "task", child, "children").read_text().split()
                    for publisher in publishers:
                        arguments = Path("/proc", publisher, "cmdline").read_bytes().split(b"\0")
                        if arguments == expected and Path("/proc", publisher, "wchan").read_text().strip() in {
                            "pipe_write",
                            "anon_pipe_write",
                        }:
                            self.assertEqual(group, os.getpgid(int(publisher)))
                            # silent-defaults: allow-contract -- Register the caller group only after matching its live anchor and exact blocked publisher; rescue rechecks the recorded anchor before admitting it; proof=repository_checks/test_acquisition_client_config.py::AcquisitionClientTests.test_actual_cleanup_report_signals_finish_before_stderr_drain
                            self.observed.setdefault(process.pid, set()).add(group)
                            return int(publisher)
                except (FileNotFoundError, ProcessLookupError):
                    continue
            time.sleep(0.01)
        raise self.failureException("actual fixed cleanup publisher did not block on the unread pipe")

    def test_print_and_inert_admission_do_not_prepare_client(self):
        ambient = self.root / "ambient-owner"
        ambient.mkdir()
        for arguments in (
            ("check-interface",),
            ("require-images",),
            ("--print", "require-images", "sha256:" + "b" * 64),
            ("--print", "run", "sha256:" + "b" * 64),
        ):
            with self.subTest(arguments=arguments):
                result = self.caller(*arguments, additions={"docker_client_directory": str(ambient)})
                self.assertEqual(0, result.returncode, msg=result.stderr)
                self.assertFalse(self.fixture.calls())
                self.assertTrue(ambient.is_dir())
        self.assertIn("image inspect", self.caller("--print", "require-images", "sha256:" + "b" * 64).stdout)

    def test_proxy_image_download_print_needs_no_interface_or_existing_image(self):
        result = self.caller("--print", "download-proxy", additions={"NMRNET_WLAN_INTERFACE_INPUT": ""})
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        self.assertEqual(["image", "pull", self.proxy_image], shlex.split(result.stdout)[-3:])
        self.assertFalse(self.fixture.calls())
        invalid = self.caller("download-proxy", "unexpected")
        self.assertEqual(2, invalid.returncode)
        self.assertFalse(self.fixture.calls())

    def test_proxy_image_download_preserves_failure_and_keeps_daemon_effects(self):
        for status in (0, 37):
            with self.subTest(status=status):
                result = self.caller(
                    "download-proxy",
                    additions={"NMRNET_WLAN_INTERFACE_INPUT": "", "NMRNET_FAKE_ENGINE_STATUS": str(status)},
                )
                self.assertEqual(status, result.returncode, msg=result.stderr)
                if status:
                    self.assertIn("registry transfer failed", result.stderr)
                    self.assertIn("Docker may retain", result.stderr)
                    self.assertIn("packages/base-images/proxy/download", result.stderr)
                else:
                    self.assertEqual("", result.stderr)
        for call in self.fixture.calls():
            self.assertEqual(["image", "pull", self.proxy_image], call["arguments"])
            self.assertEqual([], call["client_config"]["entries"])
            self.assertFalse(Path(call["client_config"]["path"]).parent.exists())

    def test_proxy_image_download_make_target_works_without_interface_selection(self):
        result = subprocess.run(
            ["make", "--no-print-directory", "packages/base-images/proxy/download"],
            cwd=self.root,
            env=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": ""},
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        self._remember(result.stderr)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual([["image", "pull", self.proxy_image]], [call["arguments"] for call in self.fixture.calls()])

    def test_missing_proxy_image_points_to_its_download_target(self):
        result = self.caller("require-images", self.proxy_image, additions={"NMRNET_FAKE_INSPECT_STATUS": "1"})
        self.assertEqual(2, result.returncode)
        self.assertIn("make packages/base-images/proxy/download", result.stderr)
        self.assertIn("without WLAN_INTERFACE binding", result.stderr)
        self.assertNotIn("preload it through an independently interface-bound process", result.stderr)
        self.assertEqual([["image", "inspect", self.proxy_image]], [call["arguments"] for call in self.fixture.calls()])

    def test_python_base_download_print_needs_no_interface_or_existing_image(self):
        result = self.fixture.run("container.sh", "--print", "download-base-image")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stderr)
        self.assertEqual(1, len(result.stdout.splitlines()))
        self.assertEqual(["image", "pull", self.worker_image], shlex.split(result.stdout)[-3:])
        self.assertFalse(self.fixture.calls())
        invalid = self.fixture.run("container.sh", "download-base-image", "unexpected")
        self.assertEqual(2, invalid.returncode)
        self.assertFalse(self.fixture.calls())

    def test_python_base_download_preserves_failure_and_keeps_daemon_effects(self):
        for status in (0, 37):
            with self.subTest(status=status):
                result = self.fixture.run(
                    "container.sh", "download-base-image", additions={"NMRNET_FAKE_ENGINE_STATUS": str(status)}
                )
                self.assertEqual(status, result.returncode, msg=result.stderr)
                if status:
                    self.assertIn("registry transfer failed", result.stderr)
                    self.assertIn("Docker may retain", result.stderr)
                    self.assertIn("packages/base-images/python/download", result.stderr)
                else:
                    self.assertEqual("", result.stderr)
        for call in self.fixture.calls():
            self.assertEqual(["image", "pull", self.worker_image], call["arguments"])
            self.assertEqual([], call["client_config"]["entries"])
            self.assertFalse(Path(call["client_config"]["path"]).parent.exists())

    def test_python_base_download_make_target_works_without_interface_selection(self):
        result = subprocess.run(
            ["make", "--no-print-directory", "packages/base-images/python/download"],
            cwd=self.root,
            env=self.fixture.environment,
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual([["image", "pull", self.worker_image]], [call["arguments"] for call in self.fixture.calls()])

    def test_next_action_keeps_json_stdout_and_requires_successful_cleanup(self):
        note = "Next, build the image."
        payload = '{"fixture":"complete"}\n'
        for worker_status, remove_status in (("0", "0"), ("37", "0"), ("0", "37")):
            with self.subTest(worker=worker_status, removal=remove_status):
                result = self.caller(
                    "--success-note",
                    note,
                    "run",
                    "sha256:" + "b" * 64,
                    additions={
                        "NMRNET_FAKE_WORKER_STATUS": worker_status,
                        "NMRNET_FAKE_REMOVE_STATUS": remove_status,
                        "NMRNET_FAKE_WORKER_STDOUT": payload,
                    },
                )
                self.assertEqual(payload, result.stdout)
                self.assertEqual(result.returncode == 0, note in result.stderr)

    def test_every_engine_phase_uses_private_client_and_preserves_live_worker_bytes(self):
        poison = self.root / "poison"
        poison.mkdir()
        (poison / "config.json").write_text('{"proxies":{"default":{"httpsProxy":"private-poison"}}}')
        forbidden = (
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
        for status in (0, 37, 143):
            with self.subTest(status=status):
                result = self.caller(
                    "run",
                    "sha256:" + "b" * 64,
                    additions=dict.fromkeys(forbidden, "private-poison")
                    | {
                        "DOCKER_CONFIG": str(poison),
                        "DOCKER_BUILDKIT": "0",
                        "NMRNET_FAKE_WORKER_STATUS": str(status),
                        "NMRNET_FAKE_WORKER_STDOUT": "live worker output\n",
                        "NMRNET_FAKE_WORKER_STDERR": "live worker error\n",
                    },
                )
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertEqual("live worker output\n", result.stdout)
                self.assertEqual("live worker error\n", result.stderr)
        for call in self.fixture.calls():
            configuration = call["client_config"]
            self.assertEqual([], configuration["entries"])
            self.assertEqual(0o700, configuration["mode"])
            self.assertEqual(os.getuid(), configuration["uid"])
            self.assertEqual("directory", configuration["kind"])
            self.assertEqual("1", call["environment"]["DOCKER_BUILDKIT"])
            self.assertTrue(set(forbidden).isdisjoint(call["environment"]))
            self.assertFalse(Path(configuration["path"]).parent.exists())
        self.assertTrue((poison / "config.json").is_file())

    def test_failed_create_exact_pair_owns_cleanup_without_start(self):
        for role in ("proxy", "worker"):
            with self.subTest(role=role):
                before = len(self.fixture.calls())
                result = self.caller(
                    "run",
                    "sha256:" + "b" * 64,
                    additions={
                        "NMRNET_FAKE_CREATE_ROLE": role,
                        "NMRNET_FAKE_CREATE_OUTPUT": "exact",
                        "NMRNET_FAKE_CREATE_STATUS": "23",
                    },
                )
                self.assertEqual(23, result.returncode)
                calls = [call["arguments"] for call in self.fixture.calls()[before:]]
                # silent-defaults: allow-contract -- Each declared create role has its own expected fixture ID; a failed exact pair permits removal but never start; proof=repository_checks/test_acquisition_client_config.py::AcquisitionClientTests.test_failed_create_exact_pair_owns_cleanup_without_start
                identifier = ("c" if role == "proxy" else "d") * 64
                self.assertNotIn(["start", identifier], calls)
                self.assertNotIn(["start", "--attach", identifier], calls)
                self.assertIn(["rm", "--force", identifier], calls)
                self.assertIn("create client failed (status 23)", result.stderr)

    def test_silent_preflight_status_keeps_private_cause_after_config_cleanup(self):
        allocation = (
            '() { local selected; if selected=$(command mktemp "$@"); then :; else return "$?"; fi; '
            'if [[ $# == 2 && "$1" == -d && "$2" == /tmp/nmrnet-proxy.XXXXXXXX ]]; then '
            'printf "%s\\n" "$selected" > "$NMRNET_ACQUISITION_SCRATCH_OBSERVATION"; fi; '
            'printf "%s\\n" "$selected"; }'
        )
        observation = self.root / "preflight-scratch"
        canary = "private-preflight-native-cause"
        for status in (130, 143):
            with self.subTest(status=status):
                observation.unlink(missing_ok=True)
                before = len(self.fixture.calls())
                result = self.caller(
                    "require-images",
                    "sha256:" + "b" * 64,
                    additions={
                        "BASH_FUNC_mktemp%%": allocation,
                        "NMRNET_ACQUISITION_SCRATCH_OBSERVATION": str(observation),
                        "NMRNET_FAKE_INSPECT_STATUS": str(status),
                        "NMRNET_FAKE_INSPECT_PRIVATE_ERROR": canary,
                    },
                )
                self.assertEqual(status, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual("", result.stderr)
                scratch = Path(observation.read_text().strip())
                self.assertEqual(Path("/tmp"), scratch.parent)
                self.assertTrue(scratch.name.startswith("nmrnet-proxy."))
                self.retained.add(scratch)
                self.assertEqual(0o700, scratch.stat().st_mode & 0o777)
                diagnostic = scratch / "preload-1.stderr"
                self.assertTrue(diagnostic.is_file())
                self.assertFalse(diagnostic.is_symlink())
                self.assertEqual(0o600, diagnostic.stat().st_mode & 0o777)
                self.assertEqual((canary + "\n").encode(), diagnostic.read_bytes())
                calls = self.fixture.calls()[before:]
                self.assertEqual([["image", "inspect", "sha256:" + "b" * 64]], [call["arguments"] for call in calls])
                self.assertFalse(Path(calls[0]["client_config"]["path"]).parent.exists())

    def test_poll_exhaustion_retains_proxy_log_cause_and_primary_status(self):
        # This new outer probe bound covers the existing 30-second metadata
        # owner plus cleanup. It is not an acquisition/readiness policy change.
        result = self.fixture.run(
            "docker.sh",
            "run",
            "sha256:" + "b" * 64,
            additions=self.environment
            | {
                "NMRNET_FAKE_PROXY_NO_READY": "1",
                "NMRNET_FAKE_PROXY_LOG_STATUS": "37",
            },
            timeout=35,
        )
        self._remember(result.stderr)
        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("proxy did not become ready", result.stderr)
        self.assertIn("logs (status 37)", result.stderr)
        self.assertNotIn("private native fixture output", result.stderr)
        calls = [call["arguments"] for call in self.fixture.calls()]
        self.assertEqual(100, calls.count(["inspect", "--format", "{{.State.Running}}", "c" * 64]))
        self.assertEqual(1, calls.count(["logs", "c" * 64]))
        self.assertLess(calls.index(["logs", "c" * 64]), calls.index(["rm", "--force", "c" * 64]))
        self.assertEqual(1, sum(call[0] == "create" for call in calls))
        scratch = Path((self.root / "proxy-cid-path").read_text()).parent
        self.assertEqual(b"private native fixture output\n", (scratch / "readiness/logs.stderr").read_bytes())
        self.assertEqual(b"37\n", (scratch / "readiness/log-status").read_bytes())
        self.assertEqual(b"", (scratch / "readiness/logs.stdout").read_bytes())
        self.assertIn(str(scratch), result.stderr)
        self.assertFalse(Path(self.fixture.calls()[-1]["client_config"]["path"]).parent.exists())

    def test_actual_create_cancel_never_erases_disagreeing_or_nul_stdout(self):
        for role in ("proxy", "worker"):
            for kind in ("empty", "disagree", "nul"):
                with self.subTest(role=role, kind=kind):
                    (self.root / "create-held").unlink(missing_ok=True)
                    before = len(self.fixture.calls())
                    process, _, _ = self._start(
                        {
                            "NMRNET_FAKE_CREATE_ROLE": role,
                            "NMRNET_FAKE_CREATE_OUTPUT": kind,
                            "NMRNET_FAKE_CREATE_STATUS": "0",
                            "NMRNET_FAKE_HOLD_CREATE": "1",
                        }
                    )
                    client = self._observe_client(process, self.root / "create-held")
                    # silent-defaults: allow-contract -- Within these empty/disagree/NUL cases, both roles use their declared IDs and only empty stdout permits CID-only cleanup after cancellation; proof=repository_checks/test_acquisition_client_config.py::AcquisitionClientTests.test_actual_create_cancel_never_erases_disagreeing_or_nul_stdout
                    identifier = ("c" if role == "proxy" else "d") * 64
                    if kind == "empty":
                        # The existing authority accepts a complete 64-byte
                        # EOF receipt even when stdout was never published.
                        receipt = Path((self.root / (role + "-cid-path")).read_text())
                        receipt.write_bytes(identifier.encode())
                        self.assertEqual(64, receipt.stat().st_size)
                    process.send_signal(signal.SIGTERM)
                    stdout, stderr = process.communicate(timeout=5)
                    self._remember(stderr.decode())
                    self.assertEqual(143, process.returncode)
                    self.assertEqual(b"", stdout)
                    self.assertEqual(b"", stderr)
                    self.assertFalse(Path("/proc", str(client)).exists())
                    calls = [call["arguments"] for call in self.fixture.calls()[before:]]
                    if kind == "empty":
                        self.assertIn(["rm", "--force", identifier], calls)
                    else:
                        self.assertNotIn(["rm", "--force", identifier], calls)
                        scratch = Path((self.root / (role + "-cid-path")).read_text()).parent
                        self.assertTrue(scratch.is_dir())
                        expected = b"e" * 64 + b"\n" if kind == "disagree" else identifier.encode() + b"\0"
                        self.assertEqual(expected, (scratch / (role + "-create/stdout")).read_bytes())

    def test_unavailable_or_nonnumeric_stat_cannot_admit_create_output(self):
        self._intercept("stat")
        for fault in ("failure", "nonnumeric"):
            with self.subTest(fault=fault):
                before = len(self.fixture.calls())
                result = self.caller("run", "sha256:" + "b" * 64, additions={"NMRNET_ACQUISITION_STAT_FAULT": fault})
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertFalse(any(call["arguments"][0] in {"start", "rm"} for call in self.fixture.calls()[before:]))
                self.assertIn("not admitted", result.stderr)

    def test_exact_limit_stdout_is_rejected_before_start(self):
        result = self.caller(
            "run",
            "sha256:" + "b" * 64,
            additions={
                "NMRNET_FAKE_CREATE_ROLE": "proxy",
                "NMRNET_FAKE_CREATE_OUTPUT": "cap",
                "NMRNET_FAKE_CREATE_STATUS": "0",
            },
        )
        self.assertEqual(2, result.returncode, msg=result.stderr)
        self.assertFalse(any(call["arguments"][0] in {"start", "rm"} for call in self.fixture.calls()))
        scratch = Path((self.root / "proxy-cid-path").read_text()).parent
        self.assertEqual(4096, (scratch / "proxy-create/stdout").stat().st_size)

    def test_readiness_distinguishes_stopped_from_unobserved_state_and_log_failure(self):
        for state, inspect_status, logs in (("false", "0", "0"), ("true", "37", "23"), ("garbage", "0", "0")):
            with self.subTest(state=state, inspect_status=inspect_status, logs=logs):
                before = len(self.fixture.calls())
                result = self.caller(
                    "run",
                    "sha256:" + "b" * 64,
                    additions={
                        "NMRNET_FAKE_PROXY_NO_READY": "1",
                        "NMRNET_FAKE_PROXY_STATE": state,
                        "NMRNET_FAKE_PROXY_INSPECT_STATUS": inspect_status,
                        "NMRNET_FAKE_PROXY_LOG_STATUS": logs,
                    },
                )
                self.assertEqual(2, result.returncode, msg=result.stderr)
                if state == "false":
                    self.assertIn("proxy stopped before becoming ready", result.stderr)
                else:
                    self.assertIn("running state could not be verified", result.stderr)
                    self.assertNotIn("proxy stopped", result.stderr)
                self.assertNotIn("private native fixture output", result.stderr)
                if logs != "0":
                    self.assertIn("logs (status 23)", result.stderr)
                self.assertEqual(1, sum(call["arguments"][0] == "create" for call in self.fixture.calls()[before:]))
                scratch = Path((self.root / "proxy-cid-path").read_text()).parent
                self.assertEqual(b"private native fixture output\n", (scratch / "readiness/logs.stderr").read_bytes())

    def test_unknown_removal_startup_retains_all_state_and_starts_no_next_owner(self):
        self._intercept("mv")
        result = self.caller("run", "sha256:" + "b" * 64, additions={"NMRNET_ACQUISITION_READY_FAULT": "1"})
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertTrue((self.root / "ready-fault").is_file())
        self.assertIn("Cannot confirm acquisition native client retirement", result.stderr)
        self.assertFalse(any(call["arguments"][0] == "rm" for call in self.fixture.calls()))
        scratch = Path((self.root / "worker-cid-path").read_text()).parent
        self.assertEqual(b"c" * 64 + b"\n", (scratch / "proxy.cid").read_bytes())
        self.assertEqual(b"d" * 64 + b"\n", (scratch / "worker.cid").read_bytes())
        self.assertTrue(Path(self.fixture.calls()[-1]["client_config"]["path"]).is_dir())

    def test_ordinary_post_start_corruption_still_reports_cause_even_at_status143(self):
        result = self.caller(
            "run",
            "sha256:" + "b" * 64,
            additions={
                "NMRNET_FAKE_CORRUPT_COMPLETED_WORKER_RECEIPT": "damaged-started-worker",
                "NMRNET_FAKE_WORKER_STATUS": "143",
            },
        )
        self.assertEqual(143, result.returncode)
        self.assertTrue((self.root / "attach-completed").is_file())
        self.assertIn("worker removal", result.stderr)
        self.assertIn("not admitted", result.stderr)
        self.assertEqual(
            [["rm", "--force", "c" * 64]], [call["arguments"] for call in self.fixture.calls() if call["arguments"][0] == "rm"]
        )
        receipt = Path((self.root / "worker-cid-path").read_text())
        self.assertEqual(b"damaged-started-worker\n", receipt.read_bytes())
        self.assertTrue(receipt.parent.is_dir())

    def test_actual_attach_interrupt_finishes_without_draining_full_stderr(self):
        process, descriptor, prefill = self._start({"NMRNET_FAKE_HOLD_ATTACH": "1"}, full_stderr=True)
        client = self._observe_client(process, self.root / "attach-ready")
        self._finish_full_pipe(process, descriptor, prefill, signal.SIGINT, 130)
        self.assertEqual(str(client), (self.root / "attach-retired").read_text())
        self.assertFalse(Path("/proc", str(client)).exists())
        self.assertEqual(
            [["rm", "--force", "d" * 64], ["rm", "--force", "c" * 64]],
            [call["arguments"] for call in self.fixture.calls() if call["arguments"][0] == "rm"],
        )

    def test_actual_cleanup_report_signals_finish_before_stderr_drain(self):
        for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupt=interrupt):
                (self.root / "attach-completed").unlink(missing_ok=True)
                process, descriptor, prefill = self._start(
                    {
                        "NMRNET_FAKE_CORRUPT_COMPLETED_WORKER_RECEIPT": "damaged-started-worker",
                    },
                    full_stderr=True,
                )
                deadline = time.monotonic() + 5
                while not (self.root / "attach-completed").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue((self.root / "attach-completed").exists())
                scratch = Path((self.root / "worker-cid-path").read_text()).parent
                publisher = self._wait_report(process, scratch)
                self._finish_full_pipe(process, descriptor, prefill, interrupt, status)
                self.assertFalse(Path("/proc", str(publisher)).exists())
                self.assertEqual(b"damaged-started-worker\n", (scratch / "worker.cid").read_bytes())
                self.assertFalse(Path(self.fixture.calls()[-1]["client_config"]["path"]).parent.exists())

    def test_failed_private_config_cleanup_preserves_primary_and_attempt_truth(self):
        self._intercept("rm")
        for status, fault in ((0, "retain"), (37, "retain"), (0, "delete")):
            with self.subTest(status=status, fault=fault):
                result = self.caller(
                    "run",
                    "sha256:" + "b" * 64,
                    additions={
                        "NMRNET_FAKE_WORKER_STATUS": str(status),
                        "NMRNET_ACQUISITION_RM_FAULT": fault,
                    },
                )
                # silent-defaults: allow-contract -- A nonzero worker status wins; otherwise failed config removal returns 41, with actual retain/delete effects checked separately; proof=repository_checks/test_acquisition_client_config.py::AcquisitionClientTests.test_failed_private_config_cleanup_preserves_primary_and_attempt_truth
                self.assertEqual(status or 41, result.returncode)
                self.assertIn("configuration removal; attempted path", result.stderr)
                self.assertNotIn("configuration retained", result.stderr)
                directory = Path(self.fixture.calls()[-1]["client_config"]["path"]).parent
                self.assertEqual(fault == "retain", directory.exists())

    def test_out_of_range_new_create_status_cannot_wrap_to_success_or_publish_cid(self):
        result = self.caller(
            "run",
            "sha256:" + "b" * 64,
            additions={
                "NMRNET_FAKE_CREATE_ROLE": "proxy",
                "NMRNET_FAKE_CREATE_OUTPUT": "exact",
                "NMRNET_FAKE_CREATE_STATUS": "256",
            },
        )
        self.assertEqual(97, result.returncode)
        self.assertFalse((self.root / "proxy-cid-path").exists())
        self.assertEqual({}, json.loads((self.root / "containers.json").read_text()))
        self.assertFalse(any(call["arguments"][0] in {"start", "rm"} for call in self.fixture.calls()))

    def _checker_inputs(self):
        # Download hands its supported PID to the same real controller exercised
        # above; only these three additional checker inputs need fixture copies.
        for name in ("tools/checker.sh", "tools/checker.py", "requirements/checker-dependencies.json"):
            (self.root / name).write_bytes((ROOT / name).read_bytes())

    def test_checker_download_signals_reach_controller_before_pipe_drain(self):
        self._checker_inputs()
        for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupt=interrupt):
                for name in ("attach-ready", "attach-retired"):
                    (self.root / name).unlink(missing_ok=True)
                before_calls = len(self.fixture.calls())
                descriptor, writer = os.pipe()
                self.addCleanup(os.close, descriptor)
                prefill = b""
                os.set_blocking(writer, False)
                while True:
                    try:
                        count = os.write(writer, b"z" * 4096)
                        prefill += b"z" * count
                    except BlockingIOError:
                        break
                os.set_blocking(writer, True)
                try:
                    process = subprocess.Popen(
                        ["bash", str(self.root / "tools/checker.sh"), "download"],
                        env=self.environment | {"NMRNET_FAKE_HOLD_ATTACH": "1"},
                        cwd=self.root,
                        stdout=subprocess.PIPE,
                        stderr=writer,
                        start_new_session=True,
                    )
                finally:
                    os.close(writer)
                self.addCleanup(self._rescue, process)
                client = self._observe_client(process, self.root / "attach-ready")
                group = os.getpgid(client)
                arguments = Path("/proc", str(process.pid), "cmdline").read_bytes().split(b"\0")
                self.assertEqual([b"bash", str(self.root / "tools/docker.sh").encode(), b"run"], arguments[:3])
                self._finish_full_pipe(process, descriptor, prefill, interrupt, status)
                self.assertEqual(str(client), (self.root / "attach-retired").read_text())
                self.assertFalse(Path("/proc", str(client)).exists())
                self.assertFalse(Path("/proc", str(group)).exists())
                calls = self.fixture.calls()[before_calls:]
                self.assertEqual(
                    [["rm", "--force", "d" * 64], ["rm", "--force", "c" * 64]],
                    [call["arguments"] for call in calls if call["arguments"][0] == "rm"],
                )
                for call in calls:
                    self.assertFalse(Path(call["client_config"]["path"]).parent.exists())

    def test_checker_download_preserves_ordinary_worker_signal_numbers(self):
        self._checker_inputs()
        for status in (130, 143):
            with self.subTest(status=status):
                before_calls = len(self.fixture.calls())
                result = self.fixture.run(
                    "checker.sh",
                    "download",
                    additions=self.environment
                    | {
                        "NMRNET_FAKE_WORKER_STATUS": str(status),
                        "NMRNET_FAKE_WORKER_STDOUT": "checker worker output\n",
                        "NMRNET_FAKE_WORKER_STDERR": "checker worker error\n",
                    },
                )
                self._remember(result.stderr)
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertEqual("checker worker output\n", result.stdout)
                self.assertEqual("checker worker error\n", result.stderr)
                calls = self.fixture.calls()[before_calls:]
                self.assertEqual(
                    [["rm", "--force", "d" * 64], ["rm", "--force", "c" * 64]],
                    [call["arguments"] for call in calls if call["arguments"][0] == "rm"],
                )
                for call in calls:
                    self.assertFalse(Path(call["client_config"]["path"]).parent.exists())

    def test_checker_download_print_preserves_worker_request_without_effects(self):
        self._checker_inputs()
        before = sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*"))
        allocation = self.root / "unexpected-allocation"
        result = self.fixture.run(
            "checker.sh",
            "--print",
            "download",
            additions=self.environment
            | {
                "NMRNET_CHECKER_ALLOCATION_OBSERVATION": str(allocation),
                "BASH_FUNC_mktemp%%": '() { printf "unexpected allocation\\n" > "$NMRNET_CHECKER_ALLOCATION_OBSERVATION"; return 97; }',
            },
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(4, len(lines))
        proxy = shlex.split(lines[0])
        self.assertEqual(self.proxy_image, proxy[proxy.index("python3") - 1])
        self.assertEqual("never", proxy[proxy.index("--pull") + 1])
        worker = shlex.split(lines[2])
        base = "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
        self.assertEqual(
            [
                base,
                "python3",
                "-B",
                "-m",
                "tools.checker",
                "download",
                "--declaration",
                "/input/dependencies.json",
                "--directory",
                "/output/dependencies",
                "--base",
                base,
            ],
            worker[-12:],
        )
        self.assertIn(f"type=bind,src={self.root}/.work/checker,dst=/output", worker)
        for name, target in (
            ("checker.py", "/workspace/tools/checker.py"),
            ("artifacts.py", "/workspace/tools/artifacts.py"),
            ("container_guard.py", "/workspace/tools/container_guard.py"),
        ):
            self.assertIn(f"type=bind,src={self.root}/tools/{name},dst={target},readonly", worker)
        self.assertIn(
            f"type=bind,src={self.root}/requirements/checker-dependencies.json,dst=/input/dependencies.json,readonly", worker
        )
        self.assertFalse(self.fixture.calls())
        self.assertFalse(allocation.exists())
        self.assertEqual(before, sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*")))

    def test_preparation_failure_retains_cause_before_cancellable_full_pipe_report(self):
        observation = self.root / "preparation-config"
        fault = (
            '() { if [[ $# == 4 && "$1" == -m && "$2" == 700 && "$3" == -- '
            '&& "$4" == /tmp/nmrnet-docker-client.*/config ]]; then '
            'printf "%s\\n" "$4" > "$NMRNET_PREPARATION_OBSERVATION"; '
            'printf "private config setup cause\\n" >&2; return 42; fi; command mkdir "$@"; }'
        )
        process, descriptor, prefill = self._start(
            {"BASH_FUNC_mkdir%%": fault, "NMRNET_PREPARATION_OBSERVATION": str(observation)}, full_stderr=True
        )
        deadline = time.monotonic() + 5
        while not observation.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(observation.exists(), msg="actual config setup did not publish its owned path")
        configuration = Path(observation.read_text().strip())
        parent = configuration.parent
        self.retained.add(parent)
        receipt = parent / "preparation.log"
        diagnostic = (
            "Cannot prepare Docker client configuration: private config creation failed (status 42); "
            f"private state was allocated at {parent}, with capped diagnostic receipt attempted at {receipt} "
            "(up to 4096 bytes; it may be absent or incomplete).\n"
        )
        source = (self.root / "tools/docker.sh").read_text()
        start = "if inference_native 'acquisition cleanup reporting' untimed stdio bash -eu -c '"
        self.assertEqual(1, source.count(start))
        program = source.split(start)[1].split("' bash ", 1)[0].encode()
        expected = [b"bash", b"-eu", b"-c", program, b"bash", diagnostic.encode(), b"", b"0", b"", b"0", b""]
        publisher = self._observe_reporter(process, expected)
        anchor = os.getpgid(publisher)
        children = Path("/proc", str(process.pid), "task", str(process.pid), "children").read_text().split()
        self.assertEqual([anchor], [int(child) for child in children if self._anchor(int(child), process.pid) is not None])
        self.assertFalse(self.fixture.calls())
        self.assertFalse(configuration.exists())
        self.assertEqual(0o700, parent.stat().st_mode & 0o777)
        self.assertEqual(0o600, receipt.stat().st_mode & 0o777)
        self.assertEqual(b"private config setup cause\n", receipt.read_bytes())
        before = receipt.read_bytes(), receipt.stat().st_ino
        self._finish_full_pipe(process, descriptor, prefill, signal.SIGTERM, 143)
        self.assertFalse(Path("/proc", str(publisher)).exists())
        self.assertFalse(Path("/proc", str(anchor)).exists())
        self.assertTrue(parent.is_dir())
        self.assertEqual(before, (receipt.read_bytes(), receipt.stat().st_ino))
        self.assertFalse(self.fixture.calls())


def fake_command(name):
    """Intercept only the precise owned utility fault selected by a case."""
    root = Path(os.environ["NMRNET_ACQUISITION_FIXTURE_ROOT"])
    arguments = sys.argv[1:]
    destination = Path(arguments[-1])
    if name == "stat":
        if arguments[:3] == ["-c", "%s", "--"] and destination.parent.name == "proxy-create" and destination.name == "stdout":
            fault = os.environ["NMRNET_ACQUISITION_STAT_FAULT"]
            if fault == "failure":
                return 41
            if fault == "nonnumeric":
                sys.stdout.write("unavailable\n")
                return 0
            return 97
        os.execv("/usr/bin/stat", ["stat", *arguments])
    if name == "rm":
        if arguments[:2] == ["-rf", "--"] and destination.name.startswith("nmrnet-docker-client."):
            fault = os.environ["NMRNET_ACQUISITION_RM_FAULT"]
            if fault == "delete":
                subprocess.run(["/bin/rm", *arguments], check=True)
            elif fault != "retain":
                return 97
            return 41
        os.execv("/bin/rm", ["rm", *arguments])
    if name == "mv":
        calls = read_engine_calls(root / "engine.jsonl")
        if destination.name == "status.ready" and calls and calls[-1]["arguments"] == ["start", "--attach", "d" * 64]:
            if os.environ["NMRNET_ACQUISITION_READY_FAULT"] != "1":
                return 97
            (root / "ready-fault").write_text(str(destination))
            return 47
        os.execv("/bin/mv", ["mv", *arguments])
    return 97


if __name__ == "__main__":
    executable = Path(sys.argv[0]).name
    if executable in {"stat", "rm", "mv"}:
        sys.exit(fake_command(executable))
    unittest.main()
