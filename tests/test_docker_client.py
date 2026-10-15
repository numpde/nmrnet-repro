#!/usr/bin/env python3
"""Observe client authority and local lifetime without an engine or network.

The executable fixture records the real caller's environment/configuration and
models a finite run. Signal tests establish only local process-group retirement.
"""

from tools.container_guard import require_container

require_container()

import json
import os
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


class DockerClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tools").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "bin").mkdir()
        for name in ("container.sh", "docker_client.sh"):
            (self.root / "tools" / name).write_bytes((Path("tools") / name).read_bytes())
        for name in ("docker", "rm", "mv"):
            (self.root / "bin" / name).symlink_to(Path(__file__).resolve())
        self.observation = self.root / "observation.json"
        self.environment = os.environ | {
            "PATH": str(self.root / "bin") + ":" + os.environ["PATH"],
            "PYTHONPATH": str(Path.cwd()),
            "FAKE_DOCKER_OBSERVATION": str(self.observation),
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

    def test_cancel_during_birth_joins_anchor_without_dispatching_docker(self):
        """Delay only PID capture in the copied caller to schedule the real handoff."""
        launcher = self.root / "tools/container.sh"
        source = launcher.read_text()
        capture = "    native_pid=$!\n"
        self.assertEqual(1, source.count(capture))
        birth = self.root / "birth"
        delayed = '    printf \'%s\\n\' "$!" "$docker_client_directory" > "$FAKE_BIRTH_RECEIPT"\n    sleep 0.25\n' + capture
        launcher.write_text(source.replace(capture, delayed, 1))
        process = subprocess.Popen(
            ["bash", str(launcher), "test"],
            env=self.environment | {"FAKE_BIRTH_RECEIPT": str(birth)},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(self._retire_failed_probe, process)
        deadline = time.monotonic() + 5
        while not birth.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(birth.exists(), msg="caller did not reach the fork-to-PID-capture seam")
        anchor, directory = birth.read_text().splitlines()
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=6)
        self.assertEqual(143, process.returncode, msg=stderr)
        self._assert_process_retired(int(anchor))
        self.assertFalse(Path(directory).exists(), msg=stderr)
        self.assertFalse(self.observation.exists(), msg="cancelled birth dispatched Docker before parent release")

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
        self.assertIn("Cannot confirm local Docker client startup", result.stderr)
        self.assertIn("owned inputs and configuration remain", result.stderr)
        self.assertFalse(self.observation.exists())
        observed = json.loads(control.read_text())
        self._assert_process_retired(observed["anchor"])
        self.assertTrue(Path(observed["directory"]).is_dir())
        subprocess.run(["/bin/rm", "-rf", "--", observed["directory"]], check=True)

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
        self.assertIn("completion publication failed; the client status is unobserved", result.stderr)
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


def _fake_command():
    """Model only caller Docker run and owned configuration/status-file cleanup."""
    arguments = sys.argv[1:]
    name = Path(sys.argv[0]).name
    if name == "rm":
        if len(arguments) != 3 or arguments[1] != "--":
            return 97
        destination = Path(arguments[2])
        configuration = arguments[0] == "-rf" and destination.name.startswith("nmrnet-docker-client.")
        probe_output = arguments[0] == "-f" and destination.name == "ownership"
        if not (configuration or probe_output):
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
                "client-status",
                "client-status.ready",
                "client-status.release",
            }
        ):
            return 97
        destination = Path(arguments[2])
        if destination.name == "client-status" and os.environ.get("FAKE_STATUS_PUBLICATION_FAILURE") == "1":
            return 29
        publication_failure = (
            destination.name == "client-status.ready" and os.environ.get("FAKE_READY_PUBLICATION_FAILURE") == "1"
        ) or (destination.name == "client-status.release" and os.environ.get("FAKE_RELEASE_PUBLICATION_FAILURE") == "1")
        if publication_failure:
            Path(os.environ["FAKE_CLIENT_CONTROL"]).write_text(
                json.dumps(
                    {
                        "directory": str(destination.parent),
                        "anchor": int(Path(arguments[1]).read_text()),
                    }
                )
            )
            return 29
        mutation = os.environ.get("FAKE_CLIENT_STATUS_RECEIPT")
        if mutation is not None and Path(arguments[2]).name == "client-status":
            Path(arguments[1]).write_bytes({"nul": b"0\0\n", "extra-lf": b"0\n\n", "out-of-range": b"256\n"}[mutation])
        status = subprocess.run(["/bin/mv", *arguments], check=False).returncode
        if status == 0 and destination.name == "client-status" and os.environ.get("FAKE_STATUS_READ_FAILURE") == "1":
            destination.chmod(0)
        return status
    if arguments[:1] != ["--config"] or arguments[2:5] != ["--host", "unix:///var/run/docker.sock", "run"]:
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
    return int(os.environ.get("FAKE_RUN_STATUS", "0"))


if __name__ == "__main__":
    if Path(sys.argv[0]).name in {"docker", "rm", "mv"}:
        raise SystemExit(_fake_command())
    unittest.main()
