#!/usr/bin/env python3
"""Observe package build/apply client ownership without builds or acquisition.

Most stage probes use a finite controller. The composition case restores the
maintained controller with the existing finite acquisition engine. Native
clients and Review publishers are real contained child processes.
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

from repository_checks.fixtures.containers.fixture import FIXTURES, LauncherFixture, record_engine_call


class PackagesClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = LauncherFixture(self.root)
        self.addCleanup(self.fixture.dispose)
        self.retained = set()
        self.addCleanup(self._remove_retained)
        self.environment = self.fixture.environment | {
            "NMRNET_PACKAGE_FIXTURE_ROOT": str(self.root),
            "NMRNET_PACKAGE_ENGINE_MODE": "package",
        }
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
        (self.root / "controller-status").write_text("0")
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

    def _preflight_environment(self):
        """Restore only the real preflight and its exact finite image clients."""
        self.preflight_image = "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
        (self.root / "tools/docker.original.sh").replace(self.root / "tools/docker.sh")
        for name in ("rm", "mv"):
            target = self.root / "bin" / name
            target.unlink()
            target.symlink_to(Path("/bin") / name)
        return self.environment | {
            "NMRNET_WLAN_INTERFACE_INPUT": "lo",
            "NMRNET_PACKAGE_ENGINE_MODE": "preflight",
            "NMRNET_PACKAGE_PREFLIGHT_HOLD": "0",
        }

    def _retire_preflight_probe(self, process, observed, release):
        """Retire only this unreaped caller and its already-admitted anchors."""
        # Before admission, separate child groups remain outside this rescue's
        # authority; the ephemeral checker container bounds their lifetime.
        release.write_text("release\n")
        if process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            os.kill(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 4
        while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            if time.monotonic() >= deadline:
                os.killpg(process.pid, signal.SIGKILL)
                break
            time.sleep(0.01)
        deadline = time.monotonic() + 3
        while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            self.assertLess(time.monotonic(), deadline, msg="preflight caller death was not observed")
            time.sleep(0.01)
        for group, arguments in observed.items():
            with suppress(FileNotFoundError, ProcessLookupError):
                if (
                    os.getsid(group) == process.pid
                    and Path("/proc", str(group), "cmdline").read_bytes().split(b"\0") == arguments
                ):
                    os.killpg(group, signal.SIGKILL)
                    while True:
                        try:
                            os.killpg(group, 0)
                        except ProcessLookupError:
                            break
                        self.assertLess(time.monotonic(), deadline, msg="observed preflight anchor survived rescue")
                        time.sleep(0.01)
                    self.retained.add(Path(arguments[4].decode()).parent)

    def test_parent_preflight_cancellation_waits_for_controller_config_cleanup(self):
        environment = self._preflight_environment()
        ready = self.root / "preflight-inspect-ready"
        poll_ready = self.root / "preflight-poll-ready"
        removal = self.root / "preflight-config-removal"
        release = self.root / "preflight-config-release"
        fault = """() {
    if [[ $# == 3 && "$1" == -rf && "$2" == -- && -v docker_client_directory
        && -n "$docker_client_directory" && "$3" == "$docker_client_directory"
        && "$3" == /tmp/nmrnet-docker-client.* ]]; then
        printf '%s|%s\\n' "$BASHPID" "$3" > "$NMRNET_PREFLIGHT_REMOVAL.pending"
        /bin/mv -- "$NMRNET_PREFLIGHT_REMOVAL.pending" "$NMRNET_PREFLIGHT_REMOVAL"
        while [[ ! -e "$NMRNET_PREFLIGHT_RELEASE" ]]; do sleep 0.01; done
        [[ $(cat "$NMRNET_PREFLIGHT_RELEASE") == release ]] || return 97
    fi
    command rm "$@"
}"""
        polling_sleep = """() {
    if [[ $# == 1 && "$1" == 0.01 && -v operation && -v ready && -v cancelled
        && "$operation" == "acquisition pinned-image inspection" && "$ready" == true && "$cancelled" == 0 ]]; then
        printf '%s\\n' "$BASHPID" > "$NMRNET_PREFLIGHT_POLL_READY.pending"
        /bin/mv -- "$NMRNET_PREFLIGHT_POLL_READY.pending" "$NMRNET_PREFLIGHT_POLL_READY"
        command sleep 60
    else
        command sleep "$@"
    fi
}"""
        before_stages = set(self.stage.parent.iterdir())
        process = subprocess.Popen(
            ["bash", str(self.root / "tools/packages.sh"), "stage"],
            env=environment
            | {
                "NMRNET_PACKAGE_PREFLIGHT_HOLD": "1",
                "NMRNET_PREFLIGHT_REMOVAL": str(removal),
                "NMRNET_PREFLIGHT_RELEASE": str(release),
                "BASH_FUNC_rm%%": fault,
                "BASH_FUNC_sleep%%": polling_sleep,
                "NMRNET_PREFLIGHT_POLL_READY": str(poll_ready),
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        observed = {}
        try:
            self._wait_path(ready)
            client = int(ready.read_text())
            arguments = Path("/proc", str(client), "cmdline").read_bytes().split(b"\0")
            self.assertEqual(os.fsencode(self.root / "bin/docker"), arguments[1])
            self.assertEqual(b"--config", arguments[2])
            self.assertEqual(
                [b"--host", b"unix:///var/run/docker.sock", b"image", b"inspect", os.fsencode(self.preflight_image), b""],
                arguments[4:],
            )
            inner = os.getpgid(client)
            controller = int(Path("/proc", str(inner), "cmdline").read_bytes().split(b"\0")[5])
            observed[inner] = self._composition_anchor(inner, process.pid, controller)
            self.assertEqual(
                [b"bash", os.fsencode(self.root / "tools/docker.sh"), b"require-images"],
                Path("/proc", str(controller), "cmdline").read_bytes().split(b"\0")[:3],
            )
            outer = os.getpgid(controller)
            observed[outer] = self._composition_anchor(outer, process.pid, process.pid)
            self._wait_path(poll_ready)
            self.assertEqual(f"{controller}\n".encode(), poll_ready.read_bytes())
            # Observe the real foreground child before the sole parent TERM;
            # the fixture receipt alone does not establish that sleep started.
            deadline = time.monotonic() + 5
            sleeper = None
            while time.monotonic() < deadline:
                children = Path("/proc", str(controller), "task", str(controller), "children").read_text().split()
                for child in children:
                    try:
                        command = Path("/proc", child, "cmdline").read_bytes().split(b"\0")
                    except FileNotFoundError:
                        continue
                    if command == [b"sleep", b"60", b""]:
                        self.assertTrue(Path("/proc", child, "exe").samefile("/usr/bin/sleep"))
                        sleeper = int(child)
                        break
                if sleeper is not None:
                    break
                time.sleep(0.01)
            self.assertIsNotNone(sleeper, msg="controller did not enter the observed foreground poll sleep")
            self.assertEqual(outer, os.getpgid(sleeper))
            self.assertEqual(process.pid, os.getsid(sleeper))
            configuration = Path(arguments[3].decode()).parent
            self.assertEqual(0o700, stat.S_IMODE(configuration.stat().st_mode))
            os.kill(process.pid, signal.SIGTERM)
            self._wait_path(removal)
            removing_controller, attempted = removal.read_text().strip().split("|")
            self.assertEqual(controller, int(removing_controller))
            self.assertEqual(str(configuration), attempted)
            self.assertFalse(Path("/proc", str(client)).exists())
            self.assertFalse(Path("/proc", str(sleeper)).exists())
            with self.assertRaises(ProcessLookupError):
                os.killpg(inner, 0)
            deadline = time.monotonic() + 2.25
            while time.monotonic() < deadline:
                self.assertIsNone(os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG))
                self.assertEqual(observed[outer], self._composition_anchor(outer, process.pid, process.pid))
                self.assertTrue(Path("/proc", str(controller)).exists())
                self.assertTrue(configuration.is_dir())
                time.sleep(0.01)
            release.write_text("release\n")
            deadline = time.monotonic() + 5
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                self.assertLess(time.monotonic(), deadline, msg="preflight completion did not return after cleanup release")
                time.sleep(0.01)
            for group in observed:
                with self.assertRaises(ProcessLookupError):
                    os.killpg(group, 0)
            self.assertFalse(Path("/proc", str(controller)).exists())
            self.assertFalse(configuration.exists())
            stdout, stderr = process.communicate(timeout=1)
            self.assertEqual(143, process.returncode)
            self.assertEqual(b"", stdout)
            self.assertEqual(b"", stderr)
            self.assertEqual(before_stages, set(self.stage.parent.iterdir()))
            self.assertFalse((self.root / "controller-returned").exists())
            self.assertEqual(["image"], [call["arguments"][0] for call in self.fixture.calls()])
        finally:
            self._retire_preflight_probe(process, observed, release)
            process.communicate(timeout=3)

    def test_ordinary_preflight143_retains_cause_without_package_allocation(self):
        environment = self._preflight_environment()
        observation = self.root / "preflight-scratch"
        allocation = """() {
    local selected status
    if selected=$(command mktemp "$@"); then :; else status=$?; return "$status"; fi
    if [[ $# == 2 && "$1" == -d && "$2" == /tmp/nmrnet-proxy.XXXXXXXX ]]; then
        printf '%s\\n' "$selected" > "$NMRNET_PREFLIGHT_SCRATCH"
    fi
    printf '%s\\n' "$selected"
}"""
        before_stages = set(self.stage.parent.iterdir())
        result = self.caller(
            "stage",
            environment=environment
            | {
                "NMRNET_FAKE_INSPECT_STATUS": "143",
                "NMRNET_FAKE_INSPECT_PRIVATE_ERROR": "private package preflight cause",
                "NMRNET_PREFLIGHT_SCRATCH": str(observation),
                "BASH_FUNC_mktemp%%": allocation,
            },
        )
        self.assertEqual(143, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual("", result.stderr)
        scratch = Path(observation.read_text().strip())
        self.assertEqual(Path("/tmp"), scratch.parent)
        self.assertRegex(scratch.name, r"^nmrnet-proxy\.[A-Za-z0-9]+$")
        self.retained.add(scratch)
        self.assertTrue(scratch.is_dir())
        self.assertFalse(scratch.is_symlink())
        self.assertEqual(0o700, stat.S_IMODE(scratch.stat().st_mode))
        diagnostic = scratch / "preload-1.stderr"
        self.assertTrue(diagnostic.is_file())
        self.assertFalse(diagnostic.is_symlink())
        self.assertEqual(0o600, stat.S_IMODE(diagnostic.stat().st_mode))
        self.assertEqual(b"private package preflight cause\n", diagnostic.read_bytes())
        calls = self.fixture.calls()
        self.assertEqual([["image", "inspect", self.preflight_image]], [call["arguments"] for call in calls])
        self.assertFalse(Path(calls[0]["client_config"]["path"]).parent.exists())
        self.assertEqual(before_stages, set(self.stage.parent.iterdir()))
        self.assertFalse((self.root / "controller-returned").exists())

    def test_preflight_outer_ready_failure_prevents_controller_and_package_allocation(self):
        environment = self._preflight_environment()
        target = self.root / "bin/mv"
        target.unlink()
        target.symlink_to(Path(__file__).resolve())
        before_stages = set(self.stage.parent.iterdir())
        allocations = self.root / "preflight-allocations"
        allocation = """() {
    local selected status
    if selected=$(command mktemp "$@"); then :; else status=$?; return "$status"; fi
    printf '%s\\n' "$selected" >> "$NMRNET_PREFLIGHT_ALLOCATIONS"
    printf '%s\\n' "$selected"
}"""
        result = self.caller(
            "stage",
            environment=environment
            | {
                "NMRNET_PACKAGE_PREFLIGHT_READY_FAULT": "1",
                "NMRNET_PREFLIGHT_ALLOCATIONS": str(allocations),
                "BASH_FUNC_mktemp%%": allocation,
            },
        )
        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("package image preflight", result.stderr)
        self.assertIn("native anchor did not publish its exact live identity", result.stderr)
        self.assertEqual("ready-failure\n", (self.root / "ready-fault").read_text())
        self.assertFalse(self.fixture.calls())
        created = allocations.read_text().splitlines()
        self.assertEqual(1, len(created))
        directory = Path(created[0])
        self.assertEqual(Path("/tmp"), directory.parent)
        self.assertRegex(directory.name, r"^nmrnet-inference-native\.[A-Za-z0-9]+$")
        self.retained.add(directory)
        self.assertTrue(directory.is_dir())
        self.assertFalse(directory.is_symlink())
        self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
        self.assertIn(str(directory), result.stderr)
        self.assertIn("Cannot confirm inference client retirement", result.stderr)
        self.assertEqual(before_stages, set(self.stage.parent.iterdir()))
        self.assertFalse((self.root / "controller-returned").exists())

    def test_failed_apply_allocation_stdout_cannot_authorize_sentinel_removal(self):
        sentinel = self.root / "not-admitted-by-caller"
        sentinel.mkdir()
        marker = sentinel / "keep"
        marker.write_bytes(b"test-owned but not caller-owned\n")
        before = (sentinel.stat().st_ino, marker.stat().st_ino, marker.read_bytes())
        configuration = self.root / "prepared-client"
        fault = """() {
    if [[ $# == 2 && "$1" == -d && "$2" == "$NMRNET_FAILED_APPLY_TEMPLATE" ]]; then
        printf '%s\\n' "$NMRNET_APPLY_SENTINEL"
        return 28
    fi
    local selected status
    if selected=$(command mktemp "$@"); then :; else status=$?; return "$status"; fi
    if [[ $# == 2 && "$1" == -d && "$2" == /tmp/nmrnet-docker-client.XXXXXXXX ]]; then
        printf '%s\\n' "$selected" > "$NMRNET_PREPARED_CLIENT"
    fi
    printf '%s\\n' "$selected"
}"""
        result = self.caller(
            "apply",
            environment=self.apply_environment()
            | {
                "BASH_FUNC_mktemp%%": fault,
                "NMRNET_FAILED_APPLY_TEMPLATE": str(self.root / ".work/packages-stage/.apply.XXXXXXXX"),
                "NMRNET_APPLY_SENTINEL": str(sentinel),
                "NMRNET_PREPARED_CLIENT": str(configuration),
            },
        )
        self.assertEqual(28, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        self.assertIn("Cannot capture package apply inputs", result.stderr)
        self.assertIn("failed (status 28)", result.stderr)
        self.assertNotIn(str(sentinel), result.stderr)
        self.assertNotIn("private apply inputs retained", result.stderr)
        self.assertFalse(self.fixture.calls())
        self.assertTrue(configuration.is_file())
        self.assertFalse(Path(configuration.read_text().strip()).exists())
        self.assertEqual(before, (sentinel.stat().st_ino, marker.stat().st_ino, marker.read_bytes()))

    def test_failed_controller_and_evidence_removal_do_not_start_another_owner(self):
        (self.root / "controller-status").write_text("37")
        attempted = self.root / "controller-evidence-attempt"
        result = self.caller(
            "stage",
            environment=self.environment
            | {
                "NMRNET_WLAN_INTERFACE_INPUT": "lo",
                "NMRNET_PACKAGE_EVIDENCE_ATTEMPT": str(attempted),
                "BASH_FUNC_rm%%": (
                    '() { if [[ -v operation && "$operation" == "package stage controller" '
                    '&& $# == 3 && "$1" == -rf && "$2" == -- '
                    '&& "$3" == /tmp/nmrnet-inference-native.* ]]; then '
                    'printf "%s" "$3" > "$NMRNET_PACKAGE_EVIDENCE_ATTEMPT"; return 23; '
                    'fi; command rm "$@"; }'
                ),
            },
        )
        self.assertEqual(37, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        evidence = Path(attempted.read_text())
        self.assertEqual(Path("/tmp"), evidence.parent)
        self.assertRegex(evidence.name, r"^nmrnet-inference-native\.[A-Za-z0-9]+$")
        self.retained.add(evidence)
        self.assertTrue(evidence.is_dir())
        self.assertFalse(evidence.is_symlink())
        self.assertEqual(0o700, stat.S_IMODE(evidence.stat().st_mode))
        self.assertEqual(
            f"Cannot confirm inference client evidence directory removal; attempted path {evidence} (status 23).\n",
            result.stderr,
        )
        controller = int((self.root / "controller-returned").read_text())
        self.assertFalse(Path("/proc", str(controller)).exists())
        self.assertFalse(Path(self.owned_calls()[-1]["config"]).parent.exists())
        # The existing ready-publication observer records any later owner.
        self.assertFalse((self.root / "report.json").exists())

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
                # The held preflight needs its own control scratch before the
                # exact pinned-pair shim runs; package allocations stay faults.
                "BASH_FUNC_mktemp%%": (
                    '() { if [[ -v operation && "$operation" == "package image preflight" '
                    '&& $# == 2 && "$1" == -d && "$2" == /tmp/nmrnet-inference-native.XXXXXXXX ]]; then '
                    'command mktemp "$@"; return "$?"; fi; '
                    'printf "unexpected allocation\\n" > "$NMRNET_PACKAGE_ALLOCATION_OBSERVATION"; return 96; }'
                ),
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
                # silent-defaults: allow-contract -- Only the declared PRINT apply case succeeds; rejected inputs must keep the ambient directory present and make no engine calls; proof=repository_checks/test_packages_client_config.py::PackagesClientTests.test_print_and_input_rejection_do_not_prepare_or_dispatch_client
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
                # silent-defaults: allow-contract -- The closed cleanup table selects delete-then-fail or failure without deletion; directory effects are checked independently; proof=repository_checks/test_packages_client_config.py::PackagesClientTests.test_cleanup_failure_changes_only_success_and_preserves_attempt_truth
                result = self.caller(
                    "apply", environment=self.apply_environment() | {"NMRNET_PACKAGE_RM_FAULT": "delete" if delete else "retain"}
                )
                # silent-defaults: allow-contract -- The declared nonzero primary 37 wins; otherwise cleanup failure expects 41 while the attempted path and actual deletion are checked separately; proof=repository_checks/test_packages_client_config.py::PackagesClientTests.test_cleanup_failure_changes_only_success_and_preserves_attempt_truth
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
        # This observation needs no control-utility fault or ready-card shim.
        for name in ("rm", "mv"):
            target = self.root / "bin" / name
            target.unlink()
            target.symlink_to(Path("/bin") / name)
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

    def _composition_anchor(self, group, session, parent):
        """Admit a real held group for this case's observed ownership chain."""
        arguments = Path("/proc", str(group), "cmdline").read_bytes().split(b"\0")
        self.assertEqual(session, os.getsid(group))
        self.assertEqual(group, os.getpgid(group))
        self.assertEqual([b"bash", b"-c"], arguments[:2])
        self.assertIn(b"receipt=$1 parent=$2 budget=$3 timed_program=$4 deadline_signal=$5 grace=$6; shift 6", arguments[2])
        self.assertEqual(str(parent).encode(), arguments[5])
        return arguments

    def test_actual_stage_controller_finishes_removals_before_cancelled_or_numeric143_return(self):
        """The actual controller keeps its cleanup time, not an outer grace."""
        (self.root / "tools/docker.original.sh").replace(self.root / "tools/docker.sh")
        # This case observes real cleanup, without the unrelated receipt and
        # removal faults supplied by the shared fixture's Python interceptors.
        for name in ("rm", "mv"):
            target = self.root / "bin" / name
            target.unlink()
            target.symlink_to(Path("/bin") / name)
        release = self.root / "composition-remove-release"
        remove_ready = self.root / "composition-remove-ready"
        attach_ready = self.root / "attach-ready"
        for cancellation, remove_mode, hold_attach, worker_status in (
            (True, "hold", "1", "0"),
            (False, "pass", "0", "143"),
        ):
            with self.subTest(cancellation=cancellation):
                for path in (release, remove_ready, attach_ready):
                    path.unlink(missing_ok=True)
                start = len(self.fixture.calls())
                stages_before = set(self.stage.parent.iterdir())
                environment = self.environment | {
                    "NMRNET_WLAN_INTERFACE_INPUT": "lo",
                    "NMRNET_PACKAGE_ENGINE_MODE": "composition",
                    "NMRNET_PACKAGE_REMOVE_MODE": remove_mode,
                    "NMRNET_FAKE_HOLD_ATTACH": hold_attach,
                    "NMRNET_FAKE_WORKER_STATUS": worker_status,
                    "NMRNET_FAKE_WORKER_STDOUT": "composition worker stdout\n",
                    "NMRNET_FAKE_WORKER_STDERR": "composition worker stderr\n",
                }
                process = subprocess.Popen(
                    ["bash", str(self.root / "tools/packages.sh"), "stage"],
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                observed = {}
                before_capture = None
                try:
                    if cancellation:
                        deadline = time.monotonic() + 5
                        raw = b""
                        while time.monotonic() < deadline:
                            if attach_ready.exists():
                                raw = attach_ready.read_bytes()
                                if re.fullmatch(rb"[1-9][0-9]*", raw):
                                    break
                            time.sleep(0.01)
                        self.assertRegex(raw, rb"\A[1-9][0-9]*\Z")
                        client = int(raw)
                        client_arguments = Path("/proc", str(client), "cmdline").read_bytes().split(b"\0")
                        self.assertEqual([os.fsencode(sys.executable), os.fsencode(FIXTURES / "docker.py")], client_arguments[:2])
                        self.assertEqual(
                            [b"--host", b"unix:///var/run/docker.sock", b"start", b"--attach", b"d" * 64, b""],
                            client_arguments[4:],
                        )
                        self.assertEqual(b"--config", client_arguments[2])
                        group = os.getpgid(client)
                        controller = int(Path("/proc", str(group), "cmdline").read_bytes().split(b"\0")[5])
                        observed[group] = self._composition_anchor(group, process.pid, controller)
                        controller_arguments = Path("/proc", str(controller), "cmdline").read_bytes().split(b"\0")
                        self.assertEqual([b"bash", os.fsencode(self.root / "tools/docker.sh"), b"run"], controller_arguments[:3])
                        outer = os.getpgid(controller)
                        observed[outer] = self._composition_anchor(outer, process.pid, process.pid)
                        calls = self.fixture.calls()[start:]
                        build = [call for call in calls if call["arguments"][:1] == ["build"]]
                        self.assertEqual(1, len(build))
                        self.assertFalse(Path(build[0]["config"]).parent.exists())
                        self.assertNotEqual(build[0]["config"].encode(), client_arguments[3])
                        stages = set(self.stage.parent.iterdir()) - stages_before
                        self.assertEqual(1, len(stages))
                        stage = stages.pop()
                        capture = stage / ".inputs"
                        before_capture = {
                            str(path.relative_to(capture)): (
                                path.read_bytes(),
                                stat.S_IMODE(path.stat().st_mode),
                                path.stat().st_ino,
                            )
                            for path in capture.rglob("*")
                            if path.is_file()
                        }
                        self.assertTrue(before_capture)
                        os.kill(process.pid, signal.SIGTERM)
                        self._wait_path(remove_ready)
                        removal = json.loads(remove_ready.read_text())
                        self.assertEqual(["rm", "--force", "d" * 64], removal["arguments"])
                        self.assertEqual(removal["group"], os.getpgid(removal["pid"]))
                        remove_arguments = Path("/proc", str(removal["pid"]), "cmdline").read_bytes().split(b"\0")
                        self.assertEqual(
                            [
                                os.fsencode(self.root / "bin/docker"),
                                b"--config",
                                client_arguments[3],
                                b"--host",
                                b"unix:///var/run/docker.sock",
                                b"rm",
                                b"--force",
                                b"d" * 64,
                                b"",
                            ],
                            remove_arguments[1:],
                        )
                        self.assertTrue(Path("/proc", str(removal["pid"]), "exe").samefile(sys.executable))
                        observed[removal["group"]] = self._composition_anchor(removal["group"], process.pid, controller)
                        self.assertEqual(str(Path(client_arguments[3].decode())), removal["config"])
                        deadline = time.monotonic() + 2.25
                        while time.monotonic() < deadline:
                            self.assertIsNone(os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG))
                            self.assertEqual(
                                observed[removal["group"]], self._composition_anchor(removal["group"], process.pid, controller)
                            )
                            self.assertTrue(Path("/proc", str(controller)).exists())
                            time.sleep(0.01)
                        self.assertEqual(
                            {"c" * 64: "proxy", "d" * 64: "worker"}, json.loads((self.root / "containers.json").read_text())
                        )
                        release.write_text("release\n")
                    deadline = time.monotonic() + 8
                    while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                        self.assertLess(time.monotonic(), deadline, msg="composition caller did not finish")
                        time.sleep(0.01)
                    for group in observed:
                        while True:
                            try:
                                os.killpg(group, 0)
                            except ProcessLookupError:
                                break
                            self.assertLess(time.monotonic(), deadline, msg="observed native group survived caller completion")
                            time.sleep(0.01)
                    stdout, stderr = process.communicate(timeout=1)
                    self._remember(stderr.decode())
                    self.assertEqual(143, process.returncode, msg=stderr.decode())
                    self.assertEqual(b"composition worker stdout\n", stdout)
                    self.assertEqual(b"composition worker stderr\n", stderr)
                    self.assertFalse((self.root / "controller-returned").exists())
                    calls = self.fixture.calls()[start:]
                    self.assertEqual(
                        [["rm", "--force", "d" * 64], ["rm", "--force", "c" * 64]],
                        [call["arguments"] for call in calls if call["arguments"][:1] == ["rm"]],
                    )
                    self.assertEqual({}, json.loads((self.root / "containers.json").read_text()))
                    for call in calls:
                        if "client_config" in call:
                            configuration = call["client_config"]
                            self.assertIsNotNone(configuration)
                            self.assertEqual(0o700, configuration["mode"])
                            self.assertEqual([], configuration["entries"])
                            self.assertFalse(Path(configuration["path"]).parent.exists())
                    stages = set(self.stage.parent.iterdir()) - stages_before
                    self.assertEqual(1, len(stages))
                    stage = stages.pop()
                    self.assertEqual(self.identity, (stage / "image-id").read_bytes())
                    capture = stage / ".inputs"
                    self.assertEqual(0o700, stat.S_IMODE(capture.stat().st_mode))
                    after_capture = {
                        str(path.relative_to(capture)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_ino)
                        for path in capture.rglob("*")
                        if path.is_file()
                    }
                    self.assertTrue(after_capture)
                    if cancellation:
                        self.assertEqual(before_capture, after_capture)
                finally:
                    if process.returncode is None:
                        # Release only this case's known fixture barrier, then
                        # give the actual owner its normal cooperative exit.
                        release.write_text("release\n")
                        with suppress(ProcessLookupError):
                            os.kill(process.pid, signal.SIGTERM)
                        deadline = time.monotonic() + 4
                        while (
                            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.01)
                        if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                            os.killpg(process.pid, signal.SIGKILL)
                            deadline = time.monotonic() + 3
                            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
                                self.assertLess(time.monotonic(), deadline, msg="composition caller death was not observed")
                                time.sleep(0.01)
                        for group, arguments in observed.items():
                            with suppress(FileNotFoundError, ProcessLookupError):
                                if (
                                    os.getsid(group) == process.pid
                                    and Path("/proc", str(group), "cmdline").read_bytes().split(b"\0") == arguments
                                ):
                                    os.killpg(group, signal.SIGKILL)
                                    deadline = time.monotonic() + 3
                                    while True:
                                        try:
                                            os.killpg(group, 0)
                                        except ProcessLookupError:
                                            break
                                        self.assertLess(
                                            time.monotonic(), deadline, msg="observed composition group did not retire"
                                        )
                                        time.sleep(0.01)
                    remaining_stdout, remaining_stderr = process.communicate(timeout=3)
                    if sys.exc_info()[0] is not None:
                        phases = [call["arguments"] for call in self.fixture.calls()[start:]]
                        sys.stderr.write(
                            f"\ncomposition failure: cancellation={cancellation!r}; "
                            f"rescued_status={process.returncode!r}; phases={phases!r}; "
                            f"attach_ready={attach_ready.exists()}; removal_ready={remove_ready.exists()}; "
                            f"stdout={remaining_stdout!r}; stderr={remaining_stderr!r}\n"
                        )

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
        # No removal fault is selected in this publisher observation.
        target = self.root / "bin/rm"
        target.unlink()
        target.symlink_to(Path("/bin/rm"))
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
                try:
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
                finally:
                    # Retire this observed case before reusing its cards in
                    # another signal subcase; the existing authority gates stay.
                    self._retire_failed_probe(process)

    def test_full_unread_mismatch_stderr_reports_after_cleanup_and_cancels_before_drain(self):
        # The exact reporter is admitted below through its live argv and wchan.
        for name in ("rm", "mv"):
            target = self.root / "bin" / name
            target.unlink()
            target.symlink_to(Path("/bin") / name)
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
        if destination.name != "status.ready":
            os.execv("/bin/mv", ["mv", *arguments])
        package_capture = False
        preflight = False
        if destination.name == "status.ready" and (
            os.environ.get("NMRNET_PACKAGE_READY_FAULT") or os.environ.get("NMRNET_PACKAGE_PREFLIGHT_READY_FAULT")
        ):
            anchor = Path("/proc", str(os.getppid()), "cmdline").read_bytes().split(b"\0")
            package_capture = anchor[10:13] == [b"bash", b"-eu", b"-c"] and b'package_capture_inputs "$2" "$3"' in anchor[13]
            preflight = anchor[10:] == [
                b"bash",
                os.fsencode(root / "tools/docker.sh"),
                b"require-images",
                b"docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c",
                b"ghcr.io/astral-sh/uv@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d",
                b"",
            ]
        if destination.name == "status.ready" and (
            (os.environ.get("NMRNET_PACKAGE_READY_FAULT") and package_capture)
            or (os.environ.get("NMRNET_PACKAGE_PREFLIGHT_READY_FAULT") == "1" and preflight)
            or (os.environ.get("NMRNET_PACKAGE_ADMISSION_READY_FAULT") and (root / "build-returned").exists())
        ):
            with (root / "ready-fault").open("a") as stream:
                stream.write("ready-failure\n")
            return 47
        subprocess.run(["/bin/mv", *arguments], check=True)
        if destination.name == "status.ready" and (root / "controller-returned").exists():
            # A previous controller card cannot make another owner's ready
            # publication evidence for the fixed Review reporter.
            source = (root / "tools/packages.sh").read_text()
            marker = "inference_native 'package stage review reporting' untimed stdio bash -eu -c '"
            if source.count(marker) != 1:
                return 97
            program = source.split(marker, 1)[1].split("' bash ", 1)[0].encode()
            anchor = Path("/proc", str(os.getppid()), "cmdline").read_bytes().split(b"\0")
            if anchor[10:14] == [b"bash", b"-eu", b"-c", program]:
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
    mode = os.environ["NMRNET_PACKAGE_ENGINE_MODE"]
    if mode not in {"package", "composition", "preflight"}:
        return 97
    if mode == "preflight":
        images = {
            "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c",
            "ghcr.io/astral-sh/uv@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d",
        }
        if len(arguments) != 3 or arguments[:2] != ["image", "inspect"] or arguments[2] not in images:
            return 97
        hold = os.environ["NMRNET_PACKAGE_PREFLIGHT_HOLD"]
        if hold == "0":
            os.execv(sys.executable, [sys.executable, str(FIXTURES / "docker.py"), *sys.argv[1:]])
        if hold != "1":
            return 97
    if mode == "composition":
        remove_mode = os.environ["NMRNET_PACKAGE_REMOVE_MODE"]
        if remove_mode not in {"hold", "pass"}:
            return 97
        python_image = "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
        uv_image = "ghcr.io/astral-sh/uv@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d"
        routed = arguments in (
            ["image", "inspect", python_image],
            ["image", "inspect", uv_image],
            ["start", "c" * 64],
            ["start", "--attach", "d" * 64],
            ["inspect", "--format", "{{.State.Running}}", "c" * 64],
            ["logs", "c" * 64],
            ["rm", "--force", "c" * 64],
            ["rm", "--force", "d" * 64],
        )
        if len(arguments) > 5 and arguments[:2] == ["create", "--cidfile"]:
            receipt = Path(arguments[2])
            routed = (
                receipt.parent.parent == Path("/tmp")
                and receipt.parent.name.startswith("nmrnet-proxy.")
                and receipt.name in {"proxy.cid", "worker.cid"}
                and (
                    (receipt.name == "proxy.cid" and arguments[3:7] == ["--pull", "never", "--network", "host"])
                    or (receipt.name == "worker.cid" and arguments[3:5] == ["--network", "host"])
                )
                and arguments.count("--cidfile") == 1
            )
        if routed:
            if arguments == ["rm", "--force", "d" * 64] and remove_mode == "hold":
                pending = root / "composition-remove-pending"
                pending.write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "group": os.getpgrp(),
                            "arguments": arguments,
                            "config": str(config),
                        }
                    )
                )
                pending.replace(root / "composition-remove-ready")
                release = root / "composition-remove-release"
                while not release.exists():
                    time.sleep(0.01)
                if release.read_bytes() != b"release\n":
                    return 97
            # Reuse the actual finite acquisition model without double-recording
            # a call or adding a wrapper process around its native client.
            os.execv(sys.executable, [sys.executable, str(FIXTURES / "docker.py"), *sys.argv[1:]])
        admission = ["image", "inspect", "--format", "{{.Id}}", "sha256:" + "b" * 64]
        if arguments != admission:
            if not arguments or arguments[0] != "build":
                return 97
            context = Path(arguments[-1])
            if (
                context.name != "context"
                or context.parent.name != ".inputs"
                or context.parent.parent.parent != root / ".work/packages-stage"
                or re.fullmatch(r"lock\.[A-Za-z0-9]+", context.parent.parent.name) is None
                or arguments
                != [
                    "build",
                    "--builder",
                    "default",
                    "--quiet",
                    "--network",
                    "none",
                    "--pull=false",
                    "--build-arg",
                    "PYTHON_BASE=" + python_image,
                    "--build-arg",
                    "UV_IMAGE=" + uv_image,
                    "--file",
                    str(context / "containers/packages/Dockerfile.lock"),
                    str(context),
                ]
            ):
                return 97
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
    if mode == "preflight":
        for interrupt in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(interrupt, signal.SIG_IGN)
        pending = root / "preflight-inspect-ready.pending"
        pending.write_text(str(os.getpid()))
        pending.replace(root / "preflight-inspect-ready")
        while True:
            time.sleep(1)
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
    return int((root / "controller-status").read_text())


if __name__ == "__main__":
    name = Path(sys.argv[0]).name
    if name in {"docker", "rm", "mv"}:
        sys.exit(fake_command(name))
    if sys.argv[1:] == ["controller"]:
        sys.exit(fake_controller())
    unittest.main()
