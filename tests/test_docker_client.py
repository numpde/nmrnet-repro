#!/usr/bin/env python3
"""Observe client authority and local lifetime without an engine or network.

The executable fixture records the real caller's environment/configuration and
models a finite run. Signal tests establish only local process-group retirement.
"""

from tools.container_guard import require_container

require_container()

import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path


class DockerClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tools").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "bin").mkdir()
        for name in ("container.sh", "docker.sh", "docker_client.sh", "inference_native.sh"):
            (self.root / "tools" / name).write_bytes((Path("tools") / name).read_bytes())
        for name in ("docker", "rm", "mv"):
            (self.root / "bin" / name).symlink_to(Path(__file__).resolve())
        self.observation = self.root / "observation.json"
        self.environment = os.environ | {
            "PATH": str(self.root / "bin") + ":" + os.environ["PATH"],
            "PYTHONPATH": str(Path.cwd()),
            "FAKE_DOCKER_OBSERVATION": str(self.observation),
            "FAKE_INSPECTION_BEHAVIOR": "success",
            "FAKE_IMAGE_PRELOAD_BEHAVIOR": "available",
            "FAKE_PROBE_OWNERSHIP": "0",
            "FAKE_READY_PUBLICATION_FAULT": "none",
            "FAKE_RUN_STATUS": "0",
        }

    def caller(self, *arguments, environment=None):
        """Run the supported caller; the test owns its expected exit status."""
        return subprocess.run(
            ["bash", str(self.root / "tools/container.sh"), *arguments],
            env=self.environment if environment is None else environment,
            text=True,
            capture_output=True,
            timeout=12,
            check=False,
        )

    def require_images(self, behavior):
        return subprocess.run(
            [
                "bash",
                str(self.root / "tools/docker.sh"),
                "require-images",
                "docker.io/library/python:3.12.12-slim-bookworm@sha256:" + "a" * 64,
            ],
            env=self.environment | {"FAKE_IMAGE_PRELOAD_BEHAVIOR": behavior},
            text=True,
            capture_output=True,
            timeout=12,
            check=False,
        )

    def test_source_and_print_are_inert(self):
        sentinel = self.root / "ambient-config"
        sentinel.mkdir()
        environment = self.environment | {"docker_client_directory": str(sentinel)}
        result = subprocess.run(
            [
                "bash",
                "-euc",
                'before=$(trap -p); source "$1"; [[ $(trap -p) == "$before" ]]; [[ $docker_client_directory == "$2" ]]',
                "bash",
                str(self.root / "tools/docker_client.sh"),
                str(sentinel),
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        result = self.caller("--print", "test", environment=environment)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        arguments = shlex.split(result.stdout)
        self.assertEqual("/tmp/nmrnet-docker-client.PRINT/config", arguments[arguments.index("--config") + 1])
        self.assertTrue(sentinel.is_dir())
        self.assertFalse(Path("/tmp/nmrnet-docker-client.PRINT").exists())
        self.assertFalse(self.observation.exists())

    def preparation(self, root, stimulus):
        """Observe failed setup, cleanup and the next PRINT call in one shell."""
        program = (
            """
root=$2
docker_client_directory=
source "$1"
mktemp() {
    local selected
    if selected=$(command mktemp "$@"); then :; else return "$?"; fi
    printf '%s\\n' "$selected" > "$root/allocated"
    printf '%s\\n' "$selected"
}
"""
            + stimulus
            + """
if docker_client_prepare run; then exit 95; else status=$?; fi
printf '%s' "$docker_client_directory" > "$root/client-binding"
printf '%s' "$docker_client_diagnostic" > "$root/diagnostic"
docker_client_cleanup
docker_client_diagnostic=stale
docker_client_prepare print
[[ -z "$docker_client_diagnostic" && -z "$docker_client_directory" ]] || exit 94
exit "$status"
"""
        )
        result = subprocess.run(
            ["bash", "-euc", program, "bash", str(self.root / "tools/docker_client.sh"), str(root)],
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        if (root / "allocated").exists():
            directory = Path((root / "allocated").read_text().strip())
            self.assertEqual(Path("/tmp"), directory.parent)
            self.assertTrue(directory.name.startswith("nmrnet-docker-client."))
            self.addCleanup(subprocess.run, ["/bin/rm", "-rf", "--", str(directory)], check=True)
        return result

    def test_failed_preparation_preserves_cause_without_failed_output_authority(self):
        canary = "private-preparation-cause"
        for phase, status in (("allocation", 28), ("config", 42)):
            with self.subTest(phase=phase):
                root = self.root / phase
                root.mkdir()
                sentinel = root / "sentinel"
                sentinel.mkdir()
                marker = sentinel / "keep"
                marker.write_bytes(b"keep\n")
                before = (marker.read_bytes(), marker.stat().st_ino, sentinel.stat().st_ino)
                # silent-defaults: allow-contract -- Allocation and config scenarios inject failures in their respective utilities; status, private cause and untouched sentinel effects are checked separately; proof=tests/test_docker_client.py::DockerClientTests.test_failed_preparation_preserves_cause_without_failed_output_authority
                stimulus = (
                    'mktemp() { printf "%s\\n" "$root/sentinel"; printf "allocation-private-cause\\n" >&2; return 28; }'
                    if phase == "allocation"
                    else f'mkdir() {{ printf "{canary}\\n" >&2; return 42; }}'
                )
                result = self.preparation(root, stimulus)
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertEqual("", result.stdout)
                self.assertEqual("", result.stderr)
                diagnostic = (root / "diagnostic").read_text()
                self.assertEqual(b"", (root / "client-binding").read_bytes())
                self.assertEqual(before, (marker.read_bytes(), marker.stat().st_ino, sentinel.stat().st_ino))
                self.assertIn(f"status {status}", diagnostic)
                self.assertNotIn(canary, diagnostic)
                self.assertNotIn("allocation-private-cause", diagnostic)
                self.assertFalse(self.observation.exists())
                if phase == "allocation":
                    self.assertIn("no private cause receipt was established", diagnostic)
                    self.assertFalse((root / "allocated").exists())
                else:
                    directory = Path((root / "allocated").read_text().strip())
                    receipt = directory / "preparation.log"
                    self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
                    self.assertTrue(receipt.is_file())
                    self.assertFalse(receipt.is_symlink())
                    self.assertEqual(0o600, stat.S_IMODE(receipt.stat().st_mode))
                    self.assertEqual((canary + "\n").encode(), receipt.read_bytes())
                    self.assertIn(str(receipt), diagnostic)

    def test_preparation_capture_failures_stop_or_bound_mkdir_and_reset_print(self):
        for fault in ("cap", "open", "oversized"):
            with self.subTest(fault=fault):
                root = self.root / fault
                root.mkdir()
                stimulus = """
mkdir() {
    printf 'started\\n' > "$root/mkdir-started"
    printf 'oversized-private-cause:%8192s' x >&2
    return 42
}
"""
                if fault == "cap":
                    # A real hard limit prevents the helper from raising its
                    # cap. Conditional-call errexit must not dispatch mkdir.
                    stimulus += "ulimit -f 1\n"
                elif fault == "open":
                    stimulus += """
mktemp() {
    local selected
    if selected=$(command mktemp "$@"); then :; else return "$?"; fi
    printf '%s\\n' "$selected" > "$root/allocated"
    command mkdir "$selected/preparation.log"
    printf '%s\\n' "$selected"
}
"""
                result = self.preparation(root, stimulus)
                # silent-defaults: allow-contract -- Oversized cause output expects SIGXFSZ status; cap/open setup faults expect 1 before mkdir, with actual marker and receipt effects checked separately; proof=tests/test_docker_client.py::DockerClientTests.test_preparation_capture_failures_stop_or_bound_mkdir_and_reset_print
                expected = 128 + signal.SIGXFSZ if fault == "oversized" else 1
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assertEqual("", result.stdout)
                self.assertEqual("", result.stderr)
                diagnostic = (root / "diagnostic").read_text()
                self.assertEqual(b"", (root / "client-binding").read_bytes())
                self.assertIn(f"status {expected}", diagnostic)
                self.assertIn("attempted at", diagnostic)
                self.assertIn("absent or incomplete", diagnostic)
                self.assertNotIn("oversized-private-cause", diagnostic)
                directory = Path((root / "allocated").read_text().strip())
                self.assertTrue(directory.is_dir())
                receipt = directory / "preparation.log"
                if fault == "oversized":
                    self.assertEqual(b"started\n", (root / "mkdir-started").read_bytes())
                    self.assertTrue(receipt.is_file())
                    self.assertEqual(4096, receipt.stat().st_size)
                    self.assertTrue(receipt.read_bytes().startswith(b"oversized-private-cause:"))
                else:
                    self.assertFalse((root / "mkdir-started").exists())
                    self.assertEqual(fault == "open", receipt.is_dir())
                    self.assertFalse(receipt.is_file())
                self.assertFalse(self.observation.exists())

    def test_preparation_exits_cannot_remove_an_ambient_named_path(self):
        sentinel = self.root / "ambient-config"
        sentinel.mkdir()
        retained = sentinel / "retained"
        retained.write_bytes(b"owned elsewhere")
        environment = self.environment | {"docker_client_directory": str(sentinel)}
        for arguments in ((), ("pull",), ("unknown",), ("check",), ("--print", "check"), ("test", "extra")):
            with self.subTest(arguments=arguments):
                result = self.caller(*arguments, environment=environment)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(b"owned elsewhere", retained.read_bytes())
                self.assertFalse(self.observation.exists())

    def test_actual_client_sees_empty_private_config_and_fixed_environment(self):
        inherited = self.root / "inherited-docker-config"
        inherited.mkdir()
        config = inherited / "config.json"
        config.write_text(
            json.dumps({"auths": {"fixture": {"auth": "canary"}}, "proxies": {"default": {"httpProxy": "private-proxy-canary"}}})
        )
        poisoned = dict.fromkeys(
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
            "private-environment-canary",
        )
        original = config.read_bytes()
        result = self.caller("test", environment=self.environment | poisoned | {"DOCKER_CONFIG": str(inherited)})
        self.assertEqual(0, result.returncode, msg=result.stderr)
        observed = json.loads(self.observation.read_text())
        self.assertEqual(0o700, observed["config_mode"])
        self.assertEqual([], observed["config_entries"])
        self.assertEqual({"DOCKER_BUILDKIT": "1"}, observed["client_environment"])
        self.assertFalse(observed["sigint_ignored"])
        self.assertNotEqual(str(inherited), observed["config"])
        self.assertFalse(Path(observed["config"]).parent.exists())
        self.assertEqual(original, config.read_bytes())
        self.assertNotIn("private-environment-canary", result.stdout + result.stderr)

    def test_missing_run_status_fails_after_observed_dispatch_without_inventing_success(self):
        environment = self.environment.copy()
        del environment["FAKE_RUN_STATUS"]
        result = self.caller("test", environment=environment)
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        self.assertIn("KeyError: 'FAKE_RUN_STATUS'", result.stderr)
        observed = json.loads(self.observation.read_text())
        self.assertEqual([], observed["config_entries"])
        self.assertFalse(Path(observed["config"]).parent.exists())

    def test_native_failure_survives_configuration_cleanup_failure(self):
        result = self.caller(
            "test",
            environment=self.environment
            | {
                "FAKE_RUN_STATUS": "19",
                "FAKE_CLIENT_CLEANUP_FAILURE": "1",
            },
        )
        self.assertEqual(19, result.returncode, msg=result.stderr)
        self.assertIn("Cannot confirm Docker client configuration removal", result.stderr)
        config = Path(json.loads(self.observation.read_text())["config"])
        self.assertTrue(config.is_dir())
        subprocess.run(["/bin/rm", "-rf", "--", str(config.parent)], check=True)

    def test_cleanup_failure_turns_native_success_into_failure(self):
        result = self.caller("test", environment=self.environment | {"FAKE_CLIENT_CLEANUP_FAILURE": "1"})
        self.assertEqual(23, result.returncode, msg=result.stderr)
        self.assertIn("Cannot confirm Docker client configuration removal", result.stderr)
        config = Path(json.loads(self.observation.read_text())["config"])
        subprocess.run(["/bin/rm", "-rf", "--", str(config.parent)], check=True)

    def test_cleanup_failure_after_removal_does_not_claim_the_path_remains(self):
        """An rm failure can follow deletion while the caller still owns status."""
        for native_status, caller_status in ((0, 23), (19, 19)):
            with self.subTest(native_status=native_status):
                result = self.caller(
                    "test",
                    environment=self.environment
                    | {
                        "FAKE_RUN_STATUS": str(native_status),
                        "FAKE_CLIENT_CLEANUP_FAILURE": "after-removal",
                    },
                )
                self.assertEqual(caller_status, result.returncode, msg=result.stderr)
                config = Path(json.loads(self.observation.read_text())["config"])
                self.assertFalse(config.parent.exists())
                self.assertIn("Cannot confirm Docker client configuration removal; attempted path", result.stderr)
                self.assertIn(str(config.parent), result.stderr)
                self.assertIn("(status 23)", result.stderr)
                self.assertNotIn("retained path", result.stderr)

    def test_status_receipt_requires_exact_bounded_bytes(self):
        for mutation in ("nul", "extra-lf", "out-of-range"):
            with self.subTest(mutation=mutation):
                result = self.caller("test", environment=self.environment | {"FAKE_CLIENT_STATUS_RECEIPT": mutation})
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertIn("byte-exact status from 0 through 255", result.stderr)
                self.assertNotIn("ignored null byte", result.stderr)
                config = Path(json.loads(self.observation.read_text())["config"])
                self.assertFalse(config.parent.exists())

    def test_client_output_eof_does_not_end_its_execution(self):
        started = time.monotonic()
        result = self.caller("test", environment=self.environment | {"FAKE_RUN_BEHAVIOR": "closed-output"})
        self.assertEqual(19, result.returncode, msg=result.stderr)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        observed = json.loads(self.observation.read_text())
        self.assertFalse(Path("/proc", str(observed["pid"])).exists())
        self.assertFalse(Path(observed["config"]).parent.exists())

    def test_probe_metadata_failure_reports_the_operation_and_cleans_owned_state(self):
        result = self.caller("probe")
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("Cannot verify probe output ownership", result.stderr)
        self.assertEqual([], list((self.root / ".work").glob("probe.*")))
        observed = json.loads(self.observation.read_text())
        self.assertFalse(Path(observed["config"]).parent.exists())

    def test_image_inspection_failure_does_not_claim_the_image_is_missing(self):
        for behavior in ("missing", "daemon-unavailable"):
            with self.subTest(behavior=behavior):
                result = self.require_images(behavior)
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertIn("Cannot verify pinned image availability", result.stderr)
                self.assertIn("does not show whether the image is absent or the Docker daemon is unavailable", result.stderr)
                self.assertIn("If the image is missing, preload it", result.stderr)
                self.assertNotIn("the pinned image must be preloaded", result.stderr)
                self.assertNotIn("private fixture", result.stdout + result.stderr)
                observed = json.loads(self.observation.read_text())
                self.assertEqual("image-preload-inspect", observed["action"])
                self.assertEqual(behavior, observed["inspection_behavior"])
                retained = next(
                    line for line in result.stderr.splitlines() if "Acquisition private receipts retained at " in line
                )
                scratch = Path(retained.rsplit(" ", 1)[-1].strip("'."))
                self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
                self.assertEqual(b"private fixture inspection cause\n", (scratch / "preload-1.stderr").read_bytes())

    def test_cancel_during_birth_joins_anchor_without_dispatching_docker(self):
        """Hold copied PID capture until the real native cancellation trap runs."""
        launcher = self.root / "tools/container.sh"
        helper = self.root / "tools/inference_native.sh"
        source = helper.read_text()
        capture = "    leader=$!\n"
        self.assertEqual(1, source.count(capture))
        birth = self.root / "birth"
        delayed = (
            '    printf \'%s\\n\' "$!" "$docker_client_directory" > "$FAKE_BIRTH_RECEIPT.pending"\n'
            '    /bin/mv -- "$FAKE_BIRTH_RECEIPT.pending" "$FAKE_BIRTH_RECEIPT"\n'
            "    while [[ $cancelled == 0 ]]; do sleep 0.01; done\n"
        ) + capture
        helper.write_text(source.replace(capture, delayed, 1))
        process = subprocess.Popen(
            ["bash", str(launcher), "test"],
            env=self.environment | {"FAKE_BIRTH_RECEIPT": str(birth)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(self._retire_failed_probe, process)
        try:
            deadline = time.monotonic() + 5
            while not birth.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(birth.exists(), msg="caller did not reach the fork-to-PID-capture seam")
            receipt = birth.read_bytes()
            self.assertRegex(receipt, rb"\A[1-9][0-9]*\n/tmp/nmrnet-docker-client\.[A-Za-z0-9]+\n\Z")
            anchor, directory = receipt.decode("ascii").splitlines()
            process.send_signal(signal.SIGTERM)
            _, stderr = process.communicate(timeout=6)
            self.assertEqual(143, process.returncode, msg=stderr)
            self._assert_process_retired(int(anchor))
            self.assertFalse(Path(directory).exists(), msg=stderr)
            self.assertFalse(self.observation.exists(), msg="cancelled birth dispatched Docker before parent release")
        finally:
            if process.poll() is None:
                # Release the injected hold through the real cancellation trap
                # before the existing last-resort rescue can join this caller.
                process.send_signal(signal.SIGTERM)
                process.communicate(timeout=6)

    def test_failed_ready_publication_after_rename_keeps_anchor_until_join(self):
        """A visible ready receipt cannot let its failed publisher end the anchor."""
        helper = self.root / "tools/inference_native.sh"
        source = helper.read_text()
        capture = "    leader=$!\n"
        self.assertEqual(1, source.count(capture))
        for fault in ("hold", "after-effect"):
            with self.subTest(fault=fault):
                phase = self.root / (fault + ".json")
                birth = self.root / (fault + ".birth")
                environment = self.environment | {
                    "FAKE_READY_PUBLICATION_FAULT": fault,
                    "FAKE_READY_PUBLICATION_CARD": str(phase),
                    "FAKE_BIRTH_RECEIPT": str(birth),
                }
                if fault == "hold":
                    delayed = (
                        '    printf \'%s\\n\' "$!" "$docker_client_directory" > "$FAKE_BIRTH_RECEIPT.pending"\n'
                        '    /bin/mv -- "$FAKE_BIRTH_RECEIPT.pending" "$FAKE_BIRTH_RECEIPT"\n'
                        "    while [[ $cancelled == 0 ]]; do sleep 0.01; done\n"
                    ) + capture
                    helper.write_text(source.replace(capture, delayed, 1))
                else:
                    helper.write_text(source)
                process = subprocess.Popen(
                    ["bash", str(self.root / "tools/container.sh"), "test"],
                    env=environment,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                self.addCleanup(self._retire_failed_probe, process)
                try:
                    deadline = time.monotonic() + 5
                    while not phase.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(phase.exists(), msg="publisher did not complete the real ready rename")
                    observed = json.loads(phase.read_text())
                    anchor = observed["anchor"]
                    directory = Path(observed["configuration_directory"])
                    self.assertRegex(str(directory), r"\A/tmp/nmrnet-docker-client\.[A-Za-z0-9]+\Z")
                    if fault == "hold":
                        while not birth.exists() and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertTrue(birth.exists())
                        receipt = birth.read_bytes()
                        self.assertRegex(receipt, rb"\A[1-9][0-9]*\n/tmp/nmrnet-docker-client\.[A-Za-z0-9]+\n\Z")
                        self.assertEqual([str(anchor), str(directory)], receipt.decode("ascii").splitlines())
                        self.assertEqual(anchor, observed["parent"])
                        self.assertEqual(anchor, os.getpgid(observed["pid"]))
                        self.assertIsNotNone(self.native_anchor(anchor, process.pid))
                        self.assertEqual((str(anchor) + "\n").encode(), Path(observed["destination"]).read_bytes())
                        arguments = Path("/proc", str(observed["pid"]), "cmdline").read_bytes().split(b"\0")
                        self.assertEqual(
                            [b"--", os.fsencode(observed["destination"] + ".pending"), os.fsencode(observed["destination"]), b""],
                            arguments[-4:],
                        )
                        process.send_signal(signal.SIGTERM)
                    stdout, stderr = process.communicate(timeout=6)
                    # silent-defaults: allow-contract -- The held-publisher case sends actual TERM to the caller and expects 143; ordinary post-rename failure expects 1, with admitted process retirement and config removal checked independently; proof=tests/test_docker_client.py::DockerClientTests.test_failed_ready_publication_after_rename_keeps_anchor_until_join
                    self.assertEqual(143 if fault == "hold" else 1, process.returncode, msg=stderr)
                    self.assertEqual("", stdout)
                    if fault == "after-effect":
                        self.assertIn("native control publication failed; the client status is unobserved", stderr)
                    self._assert_process_retired(anchor)
                    self._assert_process_retired(observed["pid"])
                    self.addCleanup(subprocess.run, ["/bin/rm", "-rf", "--", str(directory)], check=True)
                    self.assertFalse(directory.exists(), msg=stderr)
                    self.assertFalse(self.observation.exists(), msg="failed ready publication dispatched Docker")
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGTERM)
                        process.communicate(timeout=6)

    def test_failed_ready_publication_never_dispatches_docker_and_retains_uncertain_state(self):
        control = self.root / "control.json"
        result = self.caller(
            "test",
            environment=self.environment
            | {
                "FAKE_READY_PUBLICATION_FAILURE": "1",
                "FAKE_CLIENT_CONTROL": str(control),
            },
        )
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("native anchor did not publish its exact live identity within 10 seconds", result.stderr)
        self.assertIn("owned state retained", result.stderr)
        self.assertFalse(self.observation.exists())
        observed = json.loads(control.read_text())
        self._assert_process_retired(observed["anchor"])
        self.assertTrue(Path(observed["directory"]).is_dir())
        subprocess.run(["/bin/rm", "-rf", "--", observed["directory"]], check=True)
        subprocess.run(["/bin/rm", "-rf", "--", observed["native_directory"]], check=True)

    def test_failed_parent_release_never_dispatches_docker_and_retires_admitted_anchor(self):
        control = self.root / "control.json"
        result = self.caller(
            "test",
            environment=self.environment
            | {
                "FAKE_RELEASE_PUBLICATION_FAILURE": "1",
                "FAKE_CLIENT_CONTROL": str(control),
            },
        )
        self.assertEqual(29, result.returncode, msg=result.stderr)
        self.assertIn("parent release could not be published", result.stderr)
        self.assertFalse(self.observation.exists())
        observed = json.loads(control.read_text())
        self._assert_process_retired(observed["anchor"])
        self.assertFalse(Path(observed["directory"]).exists())

    def test_unreadable_status_receipt_retires_anchor_and_a_held_descendant_before_cleanup(self):
        result = self.caller(
            "test",
            environment=self.environment
            | {
                "FAKE_STATUS_READ_FAILURE": "1",
                "FAKE_RUN_BEHAVIOR": "held-descendant",
            },
        )
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("completion receipt could not be read", result.stderr)
        observed = json.loads(self.observation.read_text())
        self._assert_process_retired(observed["pid"])
        self._assert_process_retired(observed["descendant"])
        self.assertFalse(Path(observed["config"]).parent.exists())

    def test_failed_terminal_publication_retires_anchor_and_held_descendant(self):
        """Publication failure is bounded without reading an uncommitted pending file."""
        result = self.caller(
            "test",
            environment=self.environment
            | {
                "FAKE_STATUS_PUBLICATION_FAILURE": "1",
                "FAKE_RUN_BEHAVIOR": "held-descendant",
            },
        )
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("control publication failed; the client status is unobserved", result.stderr)
        observed = json.loads(self.observation.read_text())
        self._assert_process_retired(observed["pid"])
        self._assert_process_retired(observed["descendant"])
        self.assertFalse(Path(observed["config"]).parent.exists())

    def _assert_process_retired(self, pid):
        """Allow init to reap a killed group member before inspecting disappearance."""
        process_path = Path("/proc", str(pid))
        deadline = time.monotonic() + 2
        while process_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(process_path.exists(), msg=f"owned fixture process {pid} remained live")

    def test_signals_retire_a_resistant_client_before_configuration_cleanup(self):
        for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(interrupt=interrupt):
                self._assert_cancelled_client(interrupt, status)

    def _assert_cancelled_client(self, interrupt, status):
        """Bound the probe separately and inspect actual local client disappearance."""
        self.observation.unlink(missing_ok=True)
        process = subprocess.Popen(
            ["bash", str(self.root / "tools/container.sh"), "test"],
            env=self.environment | {"FAKE_RUN_BEHAVIOR": "resistant"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(self._retire_failed_probe, process)
        deadline = time.monotonic() + 5
        while not self.observation.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.observation.exists(), msg="fake client did not reach the actual engine dispatch")
        observed = json.loads(self.observation.read_text())
        process.send_signal(interrupt)
        _, stderr = process.communicate(timeout=6)
        self.assertEqual(status, process.returncode, msg=stderr)
        self.assertFalse(Path("/proc", str(observed["pid"])).exists())
        self.assertFalse(Path(observed["config"]).parent.exists())

    def test_preparation_cause_survives_container_failure_without_engine_dispatch(self):
        observation = self.root / "preparation-config"
        fault = (
            '() { if [[ $# == 4 && "$1" == -m && "$2" == 700 && "$3" == -- '
            '&& "$4" == /tmp/nmrnet-docker-client.*/config ]]; then '
            'printf "%s\\n" "$4" > "$NMRNET_PREPARATION_OBSERVATION"; '
            'printf "private config setup cause\\n" >&2; return 42; fi; command mkdir "$@"; }'
        )
        result = self.caller(
            "test",
            environment=self.environment | {"BASH_FUNC_mkdir%%": fault, "NMRNET_PREPARATION_OBSERVATION": str(observation)},
        )
        self.assertEqual(42, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        self.assertFalse(self.observation.exists())
        configuration = Path(observation.read_text().strip())
        parent = configuration.parent
        self.addCleanup(subprocess.run, ["/bin/rm", "-rf", "--", str(parent)], check=True)
        receipt = parent / "preparation.log"
        self.assertFalse(configuration.exists())
        self.assertEqual(0o700, stat.S_IMODE(parent.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(receipt.stat().st_mode))
        self.assertEqual(b"private config setup cause\n", receipt.read_bytes())
        self.assertIn("private config creation failed (status 42)", result.stderr)
        self.assertIn(str(parent), result.stderr)
        self.assertIn(str(receipt), result.stderr)
        self.assertNotIn("private config setup cause", result.stderr)

    def _retire_failed_probe(self, process):
        """Keep a failed test from leaving its own process or fixture state live."""
        if process.poll() is None:
            if self.observation.exists():
                observed = json.loads(self.observation.read_text())
                with suppress(ProcessLookupError):
                    os.killpg(observed["process_group"], signal.SIGKILL)
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)

    def checker_inputs(self, program):
        """Supply finite inspection/preparation inputs without an engine or Git."""
        (self.root / "repository_checks").mkdir()
        (self.root / ".work/checker").mkdir(parents=True)
        (self.root / "Makefile").write_text("help:\n")
        image = "sha256:" + "a" * 64
        (self.root / ".work/checker/image-id").write_text(image + "\n")
        (self.root / "tools/check_git_inputs.sh").write_text("set -eu\n" + program)
        return image

    def native_anchor(self, group, parent):
        """Admit only an observed held anchor in this unreaped caller's session."""
        try:
            arguments = Path("/proc", str(group), "cmdline").read_bytes().split(b"\0")
            if (
                os.getsid(group) == parent
                and arguments[:2] == [b"bash", b"-c"]
                and len(arguments) > 10
                and b"receipt=$1 parent=$2 budget=$3 timed_program=$4 deadline_signal=$5 grace=$6; shift 6" in arguments[2]
                and arguments[5] == str(parent).encode()
            ):
                return arguments
        except (FileNotFoundError, ProcessLookupError):
            return None
        return None

    def unread_caller(self, lane, environment, stream):
        """Fill the actual public pipe before the supported caller starts."""
        descriptor, writer = os.pipe()
        self.addCleanup(os.close, descriptor)
        os.set_blocking(writer, False)
        prefill = b""
        while True:
            try:
                count = os.write(writer, b"z" * 4096)
                prefill += b"z" * count
            except BlockingIOError:
                break
        os.set_blocking(writer, True)
        process = subprocess.Popen(
            ["bash", str(self.root / "tools/container.sh"), lane],
            env=environment,
            stdout=writer if stream == "stdout" else subprocess.PIPE,
            stderr=writer if stream == "stderr" else subprocess.PIPE,
            start_new_session=True,
        )
        os.close(writer)
        admitted = set()
        self.addCleanup(self.rescue_observed, process, admitted)
        return process, descriptor, prefill, admitted

    def rescue_observed(self, process, admitted):
        """Rescue only already observed anchors after positive unreaped death."""
        if process.returncode is not None:
            return
        with suppress(ProcessLookupError):
            os.kill(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            if time.monotonic() >= deadline:
                os.killpg(process.pid, signal.SIGKILL)
                break
            time.sleep(0.01)
        deadline = time.monotonic() + 3
        while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            self.assertLess(time.monotonic(), deadline, msg="failed caller death was not observed")
            time.sleep(0.01)
        for group in admitted:
            if self.native_anchor(group, process.pid) is not None:
                with suppress(ProcessLookupError):
                    os.killpg(group, signal.SIGKILL)
                while True:
                    try:
                        os.killpg(group, 0)
                    except ProcessLookupError:
                        break
                    self.assertLess(time.monotonic(), deadline, msg="observed anchor did not retire")
                    time.sleep(0.01)
        process.communicate(timeout=3)

    def observe_file(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), msg=f"actual phase did not publish {path.name}")
        return path.read_text()

    def observe_publisher(self, process, expected, admitted):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            children = Path("/proc", str(process.pid), "task", str(process.pid), "children").read_text().split()
            anchors = [int(child) for child in children if self.native_anchor(int(child), process.pid) is not None]
            admitted.update(anchors)
            for group in anchors:
                child = str(group)
                try:
                    publishers = Path("/proc", child, "task", child, "children").read_text().split()
                    for publisher in publishers:
                        arguments = Path("/proc", publisher, "cmdline").read_bytes().split(b"\0")
                        if arguments == expected and Path("/proc", publisher, "wchan").read_text().strip() in {
                            "pipe_write",
                            "anon_pipe_write",
                        }:
                            self.assertEqual(group, os.getpgid(int(publisher)))
                            self.assertEqual([group], anchors)
                            return int(publisher), group
                except (FileNotFoundError, ProcessLookupError):
                    continue
            time.sleep(0.01)
        raise self.failureException("actual fixed publisher did not block on the unread public pipe")

    def finish_unread(self, process, descriptor, prefill, admitted):
        os.kill(process.pid, signal.SIGTERM)
        # Keep the caller unreaped through actual death and held-group
        # disappearance. An assertion failure can still use observed rescue.
        deadline = time.monotonic() + 6
        while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            self.assertLess(time.monotonic(), deadline, msg="sole signal did not finish the caller before drain")
            time.sleep(0.01)
        for group in admitted:
            while True:
                try:
                    os.killpg(group, 0)
                except ProcessLookupError:
                    break
                self.assertLess(time.monotonic(), deadline, msg="observed native group did not retire before drain")
                time.sleep(0.01)
        self.assertEqual(143, process.wait(timeout=1))
        stdout, stderr = process.communicate(timeout=3)
        self.assertIn(stdout, (None, b""))
        self.assertIn(stderr, (None, b""))
        os.set_blocking(descriptor, False)
        drained = b""
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            drained += chunk
        self.assertEqual(prefill, drained, msg="actual cancellation published new public bytes")

    def test_inspection_cancel_retains_private_cause_and_stops_before_worker(self):
        self.checker_inputs("exit 96\n")
        environment = self.environment | {"FAKE_INSPECTION_BEHAVIOR": "hold"}
        process, descriptor, prefill, admitted = self.unread_caller("test/repository", environment, "stderr")
        observed = json.loads(self.observe_file(self.observation))
        group = observed["process_group"]
        anchor = self.native_anchor(group, process.pid)
        self.assertIsNotNone(anchor)
        admitted.add(group)
        self.finish_unread(process, descriptor, prefill, admitted)
        self._assert_process_retired(group)
        self.addCleanup(subprocess.run, ["/bin/rm", "-rf", "--", str(Path(os.fsdecode(anchor[4])).parent)], check=True)
        self.assertEqual("inspect", observed["action"])
        self._assert_process_retired(observed["pid"])
        self.assertFalse(Path(observed["config"]).parent.exists())
        receipts = list((self.root / ".work/checker").glob(".inspect.*"))
        self.assertEqual(1, len(receipts))
        receipt = receipts[0]
        self.assertEqual(0o700, stat.S_IMODE(receipt.stat().st_mode))
        diagnostic = receipt / "diagnostic"
        self.assertEqual(0o600, stat.S_IMODE(diagnostic.stat().st_mode))
        self.assertEqual(b"private inspection cause\n", diagnostic.read_bytes())
        self.assertFalse(list((self.root / ".work").glob("repository-check-inputs.*")))
        subprocess.run(["/bin/rm", "-rf", "--", str(receipt)], check=True)

    def test_held_git_preparation_preserves_cause_and_cancels_before_worker(self):
        phase = self.root / "preparation-phase"
        self.checker_inputs(
            '[[ $# == 2 && "$1" == "$NMRNET_PREPARATION_ROOT" && "$2" == "$1"/.work/repository-check-inputs.*/git ]] || exit 97\n'
            'printf "private Git preparation cause\\n" >&2\n'
            'printf "%s\\n%s\\n" "$BASHPID" "$2" > "$NMRNET_PREPARATION_PHASE.pending"\n'
            '/bin/mv -- "$NMRNET_PREPARATION_PHASE.pending" "$NMRNET_PREPARATION_PHASE"\n'
            'if [[ "$NMRNET_PREPARATION_BEHAVIOR" == failure ]]; then exit 37; fi\n'
            '[[ "$NMRNET_PREPARATION_BEHAVIOR" == hold ]] || exit 97\n'
            "while :; do sleep 60; done\n"
        )
        for behavior in ("failure", "hold"):
            with self.subTest(behavior=behavior):
                phase.unlink(missing_ok=True)
                self.observation.unlink(missing_ok=True)
                environment = self.environment | {
                    "NMRNET_PREPARATION_ROOT": str(self.root),
                    "NMRNET_PREPARATION_PHASE": str(phase),
                    "NMRNET_PREPARATION_BEHAVIOR": behavior,
                }
                if behavior == "failure":
                    result = self.caller("test/repository", environment=environment)
                    self.assertEqual(37, result.returncode, msg=result.stderr)
                    self.assertNotIn("private Git preparation cause", result.stderr)
                    paths = re.findall(r"/tmp/nmrnet-inference-native\.[A-Za-z0-9]+/command.log", result.stderr)
                    self.assertEqual(1, len(paths))
                    log = Path(paths[0])
                    self.addCleanup(subprocess.run, ["/bin/rm", "-rf", "--", str(log.parent)], check=True)
                    self.assertEqual(0o700, stat.S_IMODE(log.parent.stat().st_mode))
                    self.assertEqual(0o600, stat.S_IMODE(log.stat().st_mode))
                    self.assertEqual(b"private Git preparation cause\n", log.read_bytes())
                    identifier, destination = phase.read_text().splitlines()
                else:
                    process, descriptor, prefill, admitted = self.unread_caller("test/repository", environment, "stderr")
                    identifier, destination = self.observe_file(phase).splitlines()
                    group = os.getpgid(int(identifier))
                    anchor = self.native_anchor(group, process.pid)
                    self.assertIsNotNone(anchor)
                    admitted.add(group)
                    self.finish_unread(process, descriptor, prefill, admitted)
                    self._assert_process_retired(group)
                    log = Path(os.fsdecode(anchor[4])).parent / "command.log"
                    self.addCleanup(subprocess.run, ["/bin/rm", "-rf", "--", str(log.parent)], check=True)
                    self.assertTrue(log.read_bytes().startswith(b"private Git preparation cause\n"))
                    self.assertLessEqual(log.stat().st_size, 8388608)
                    self.assertEqual(0o700, stat.S_IMODE(log.parent.stat().st_mode))
                    self.assertEqual(0o600, stat.S_IMODE(log.stat().st_mode))
                self._assert_process_retired(int(identifier))
                self.assertFalse(Path(destination).parent.exists())
                observed = json.loads(self.observation.read_text())
                self.assertEqual("inspect", observed["action"])
                self._assert_process_retired(observed["pid"])
                self._assert_process_retired(observed["process_group"])
                self.assertFalse(Path(observed["config"]).parent.exists())
                self.assertFalse(list((self.root / ".work/checker").glob(".inspect.*")))

    def test_probe_success_publisher_cancels_before_stdout_drain_and_cleans_state(self):
        environment = self.environment | {"FAKE_PROBE_OWNERSHIP": "1"}
        process, descriptor, prefill, admitted = self.unread_caller("probe", environment, "stdout")
        observed = json.loads(self.observe_file(self.observation))
        owner = f"{os.getuid()}:{os.getgid()}"
        expected = [b"bash", b"-eu", b"-c", b'printf "Probe output ownership: %s\\n" "$1"', b"bash", owner.encode(), b""]
        publisher, group = self.observe_publisher(process, expected, admitted)
        self._assert_process_retired(observed["pid"])
        self._assert_process_retired(observed["process_group"])
        self.assertTrue(Path(observed["config"]).parent.is_dir())
        probe = Path(observed["probe_output"])
        self.assertEqual(
            owner,
            subprocess.run(
                ["stat", "-c", "%u:%g", str(probe / "ownership")], capture_output=True, text=True, check=True
            ).stdout.strip(),
        )
        self.finish_unread(process, descriptor, prefill, admitted)
        self._assert_process_retired(publisher)
        self._assert_process_retired(group)
        self.assertFalse(Path(observed["config"]).parent.exists())
        self.assertFalse(probe.exists())

    def test_failed_allocations_cannot_admit_stdout_as_cleanup_authority(self):
        self.checker_inputs("exit 96\n")
        sentinel = self.root / "unowned"
        sentinel.mkdir()
        marker = sentinel / "keep"
        marker.write_bytes(b"not owned by launcher\n")
        before = (sentinel.stat().st_ino, marker.stat().st_ino, marker.read_bytes())
        config_card = self.root / "allocated-client"
        fault_card = self.root / "failed-allocation"
        fault = """() {
    [[ $# == 2 && "$1" == -d ]] || return 97
    if [[ "$2" == "$NMRNET_FAILED_TEMPLATE" ]]; then
        printf '%s\\n' "$2" > "$NMRNET_FAILED_CARD"
        printf '%s\\n' "$NMRNET_UNOWNED_SENTINEL"
        printf 'private allocation cause\\n' >&2
        return 28
    fi
    local selected status
    if selected=$(command mktemp "$@"); then :; else status=$?; return "$status"; fi
    if [[ "$2" == /tmp/nmrnet-docker-client.XXXXXXXX ]]; then
        printf '%s\\n' "$selected" > "$NMRNET_ALLOCATED_CLIENT"
    fi
    printf '%s\\n' "$selected"
}"""
        for lane, template in (
            ("test/repository", self.root / ".work/checker/.inspect.XXXXXXXX"),
            ("test/repository", self.root / ".work/repository-check-inputs.XXXXXXXX"),
            ("probe", self.root / ".work/probe.XXXXXXXX"),
        ):
            with self.subTest(template=template):
                fault_card.unlink(missing_ok=True)
                self.observation.unlink(missing_ok=True)
                result = self.caller(
                    lane,
                    environment=self.environment
                    | {
                        "BASH_FUNC_mktemp%%": fault,
                        "NMRNET_FAILED_TEMPLATE": str(template),
                        "NMRNET_FAILED_CARD": str(fault_card),
                        "NMRNET_UNOWNED_SENTINEL": str(sentinel),
                        "NMRNET_ALLOCATED_CLIENT": str(config_card),
                    },
                )
                self.assertEqual(28, result.returncode, msg=result.stderr)
                self.assertEqual(str(template) + "\n", fault_card.read_text())
                self.assertEqual(before, (sentinel.stat().st_ino, marker.stat().st_ino, marker.read_bytes()))
                self.assertIn("no private cause receipt was established", result.stderr)
                self.assertNotIn("private allocation cause", result.stderr)
                self.assertEqual("", result.stdout)
                self.assertFalse(Path(config_card.read_text().strip()).exists())
                if template.name == "repository-check-inputs.XXXXXXXX":
                    self.assertEqual("inspect", json.loads(self.observation.read_text())["action"])
                else:
                    self.assertFalse(self.observation.exists())

    def test_record_metadata_failure_cleans_config_before_cancellable_public_report(self):
        self.checker_inputs("exit 96\n")
        config_card = self.root / "allocated-client"
        fault_card = self.root / "metadata-fault"
        environment = self.environment | {
            "NMRNET_RECORD_ROOT": str(self.root),
            "NMRNET_ALLOCATED_CLIENT": str(config_card),
            "NMRNET_METADATA_FAULT": str(fault_card),
            "BASH_FUNC_mktemp%%": """() {
    local selected status
    if selected=$(command mktemp "$@"); then :; else status=$?; return "$status"; fi
    if [[ $# == 2 && "$1" == -d && "$2" == /tmp/nmrnet-docker-client.XXXXXXXX ]]; then
        printf '%s\\n' "$selected" > "$NMRNET_ALLOCATED_CLIENT"
    fi
    printf '%s\\n' "$selected"
}""",
            "BASH_FUNC_stat%%": """() {
    if [[ $# == 3 && "$1" == -c && "$2" == %s && "$3" == "$NMRNET_RECORD_ROOT/.work/checker/image-id" ]]; then
        printf 'actual record metadata failure\\n' > "$NMRNET_METADATA_FAULT"
        printf '%65536s' x >&2
        return 41
    fi
    command stat "$@"
}""",
        }
        process, descriptor, prefill, admitted = self.unread_caller("test/repository", environment, "stderr")
        pending = f"Cannot run repository checks: image-id metadata at {self.root}/.work/checker/image-id could not be read (status 41).\n"
        expected = [b"bash", b"-eu", b"-c", b'printf "%s" "$1" >&2', b"bash", pending.encode(), b""]
        publisher, group = self.observe_publisher(process, expected, admitted)
        self.assertEqual("actual record metadata failure\n", fault_card.read_text())
        self.assertFalse(Path(config_card.read_text().strip()).exists())
        self.assertFalse(self.observation.exists())
        self.finish_unread(process, descriptor, prefill, admitted)
        self._assert_process_retired(publisher)
        self._assert_process_retired(group)


def _fake_command():
    """Model caller Docker run and the real shared owner's control-file effects."""
    arguments = sys.argv[1:]
    name = Path(sys.argv[0]).name
    if name == "rm":
        if len(arguments) >= 3 and arguments[:2] == ["-f", "--"]:
            paths = [Path(value) for value in arguments[2:]]
            directory = paths[0].parent
            if (
                directory.parent == Path("/tmp")
                and directory.name.startswith("nmrnet-inference-native.")
                and all(path.parent == directory and (path.name == "output" or path.name.startswith("status")) for path in paths)
            ):
                return subprocess.run(["/bin/rm", *arguments], check=False).returncode
        if len(arguments) != 3 or arguments[1] != "--":
            return 97
        destination = Path(arguments[2])
        root = Path(os.environ["FAKE_DOCKER_OBSERVATION"]).parent
        configuration = arguments[0] == "-rf" and destination.name.startswith("nmrnet-docker-client.")
        native_control = (
            arguments[0] == "-rf"
            and destination.parent == Path("/tmp")
            and destination.name.startswith("nmrnet-inference-native.")
        )
        probe_output = arguments[0] == "-f" and destination.name == "ownership"
        checker_receipt = (
            arguments[0] == "-rf" and destination.parent == root / ".work/checker" and destination.name.startswith(".inspect.")
        )
        scaffold = destination.parent.parent == root / ".work" and destination.parent.name.startswith("repository-check-inputs.")
        checker_scaffold = scaffold and (
            (arguments[0] == "-rf" and destination.name == "git") or (arguments[0] == "-f" and destination.name == "root-entries")
        )
        if not (configuration or native_control or probe_output or checker_receipt or checker_scaffold):
            return 97
        if configuration:
            cleanup_failure = os.environ.get("FAKE_CLIENT_CLEANUP_FAILURE")
            if cleanup_failure == "after-removal":
                subprocess.run(["/bin/rm", *arguments], check=True)
                return 23
            if cleanup_failure == "1":
                return 23
        return subprocess.run(["/bin/rm", *arguments], check=False).returncode
    if name == "mv":
        if (
            len(arguments) != 3
            or arguments[0] != "--"
            or Path(arguments[2]).name
            not in {
                "status",
                "status.ready",
                "status.release",
            }
        ):
            return 97
        destination = Path(arguments[2])
        ready_fault = os.environ["FAKE_READY_PUBLICATION_FAULT"]
        if ready_fault not in {"none", "hold", "after-effect"}:
            return 97
        if destination.name == "status.ready" and ready_fault != "none" and "FAKE_READY_PUBLICATION_CARD" not in os.environ:
            return 97
        if destination.parent.parent != Path("/tmp") or not destination.parent.name.startswith("nmrnet-inference-native."):
            return 97
        ready = Path(arguments[1]) if destination.name == "status.ready" else destination.parent / "status.ready"
        anchor = int(ready.read_text())
        anchor_arguments = Path("/proc", str(anchor), "cmdline").read_bytes().split(b"\0")
        if (
            anchor_arguments[:2] != [b"bash", b"-c"]
            or b"receipt=$1 parent=$2 budget=$3 timed_program=$4 deadline_signal=$5 grace=$6; shift 6" not in anchor_arguments[2]
        ):
            return 97
        payload = anchor_arguments[10:-1]
        # These faults still target the original Docker-run phase. A later
        # reporter is another native invocation and must not inherit its fault.
        # silent-defaults: allow-contract -- An absent Docker token records None, keeping such payloads outside the original Docker-run fault target, whose admitted anchor retirement is checked separately; proof=tests/test_docker_client.py::DockerClientTests.test_failed_ready_publication_never_dispatches_docker_and_retains_uncertain_state
        engine = payload.index(b"docker") if b"docker" in payload else None
        run_phase = engine is not None and payload[engine + 1 : engine + 6 : 2] == [b"--config", b"--host", b"run"]
        if run_phase:
            if payload[engine + 4] != b"unix:///var/run/docker.sock":
                return 97
            configuration_directory = Path(os.fsdecode(payload[engine + 2])).parent
        if not run_phase:
            return subprocess.run(["/bin/mv", *arguments], check=False).returncode
        if destination.name == "status" and os.environ.get("FAKE_STATUS_PUBLICATION_FAILURE") == "1":
            return 29
        publication_failure = (
            destination.name == "status.ready" and os.environ.get("FAKE_READY_PUBLICATION_FAILURE") == "1"
        ) or (destination.name == "status.release" and os.environ.get("FAKE_RELEASE_PUBLICATION_FAILURE") == "1")
        if publication_failure:
            Path(os.environ["FAKE_CLIENT_CONTROL"]).write_text(
                json.dumps(
                    {
                        "directory": str(configuration_directory),
                        "native_directory": str(destination.parent),
                        "anchor": anchor,
                    }
                )
            )
            return 29
        mutation = os.environ.get("FAKE_CLIENT_STATUS_RECEIPT")
        if mutation is not None and Path(arguments[2]).name == "status":
            Path(arguments[1]).write_bytes({"nul": b"0\0\n", "extra-lf": b"0\n\n", "out-of-range": b"256\n"}[mutation])
        status = subprocess.run(["/bin/mv", *arguments], check=False).returncode
        if status == 0 and destination.name == "status.ready" and ready_fault != "none":
            phase = Path(os.environ["FAKE_READY_PUBLICATION_CARD"])
            pending_phase = phase.with_suffix(".pending")
            pending_phase.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "parent": os.getppid(),
                        "anchor": anchor,
                        "destination": str(destination),
                        "configuration_directory": str(configuration_directory),
                    }
                )
            )
            pending_phase.replace(phase)
            if ready_fault == "after-effect":
                return 29
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                time.sleep(0.01)
        if status == 0 and destination.name == "status" and os.environ.get("FAKE_STATUS_READ_FAILURE") == "1":
            destination.chmod(0)
        return status
    inspection = len(arguments) == 9 and arguments[4:8] == ["image", "inspect", "--format", "{{.Id}}"]
    preload_inspection = len(arguments) == 7 and arguments[4:6] == ["image", "inspect"]
    if (
        arguments[:1] != ["--config"]
        or arguments[2:4] != ["--host", "unix:///var/run/docker.sock"]
        or (not inspection and not preload_inspection and arguments[4:5] != ["run"])
    ):
        return 97
    config = Path(arguments[1])
    observed = {
        "pid": os.getpid(),
        "process_group": os.getpgrp(),
        "sigint_ignored": signal.getsignal(signal.SIGINT) == signal.SIG_IGN,
        "config": str(config),
        "config_mode": stat.S_IMODE(config.stat().st_mode),
        "config_entries": sorted(path.name for path in config.iterdir()),
        "client_environment": {
            key: value
            for key, value in os.environ.items()
            if key.startswith(("DOCKER_", "BUILDX_")) or key.lower().endswith("_proxy")
        },
    }
    behavior = os.environ.get("FAKE_RUN_BEHAVIOR")
    if behavior == "resistant":
        for interrupt in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(interrupt, signal.SIG_IGN)
    # Write complete evidence only after the resistant behavior is installed.
    observation = Path(os.environ["FAKE_DOCKER_OBSERVATION"])
    staged = observation.with_suffix(".pending")
    if preload_inspection:
        if arguments[6] != "docker.io/library/python:3.12.12-slim-bookworm@sha256:" + "a" * 64:
            return 97
        behavior = os.environ["FAKE_IMAGE_PRELOAD_BEHAVIOR"]
        if behavior not in {"available", "missing", "daemon-unavailable"}:
            return 97
        observed.update(action="image-preload-inspect", inspection_behavior=behavior)
        staged.write_text(json.dumps(observed))
        staged.replace(observation)
        if behavior == "available":
            os.write(1, (arguments[6] + "\n").encode())
            return 0
        os.write(2, b"private fixture inspection cause\n")
        if behavior == "missing":
            return 1
        return 125
    if inspection:
        if arguments[8] != "sha256:" + "a" * 64:
            return 97
        behavior = os.environ["FAKE_INSPECTION_BEHAVIOR"]
        if behavior not in {"success", "hold"}:
            return 97
        observed["action"] = "inspect"
        if behavior != "success":
            os.write(2, b"private inspection cause\n")
        staged.write_text(json.dumps(observed))
        staged.replace(observation)
        if behavior == "hold":
            while True:
                time.sleep(60)
        os.write(1, (arguments[8] + "\n").encode())
        return 0
    probe_ownership = os.environ["FAKE_PROBE_OWNERSHIP"]
    if probe_ownership not in {"0", "1"}:
        return 97
    if probe_ownership == "1":
        mounts = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--mount"]
        outputs = [mount for mount in mounts if mount.endswith(",dst=/output")]
        if len(outputs) != 1 or not outputs[0].startswith("type=bind,src="):
            return 97
        output = Path(outputs[0].removeprefix("type=bind,src=").removesuffix(",dst=/output"))
        if output.parent != observation.parent / ".work" or not output.name.startswith("probe."):
            return 97
        (output / "ownership").write_text("probe ownership fixture\n")
        observed["probe_output"] = str(output)
    staged.write_text(json.dumps(observed))
    staged.replace(observation)
    if behavior == "held-descendant":
        # The descendant deliberately outlives its client and holds both pipes;
        # the caller's admitted group must retire it before scratch cleanup.
        descendant = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"])
        observed["descendant"] = descendant.pid
        staged.write_text(json.dumps(observed))
        staged.replace(observation)
        return 19
    if behavior == "resistant":
        while True:
            time.sleep(60)
    if behavior == "closed-output":
        os.close(1)
        os.close(2)
        time.sleep(0.25)
        return 19
    return int(os.environ["FAKE_RUN_STATUS"])


if __name__ == "__main__":
    if Path(sys.argv[0]).name in {"docker", "rm", "mv"}:
        raise SystemExit(_fake_command())
    unittest.main()
