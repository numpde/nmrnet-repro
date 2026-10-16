#!/usr/bin/env python3
"""Observe baseline metadata admission through finite offline native commands.

The executable mode is required by its docker/rm PATH fixture, like
tests/test_docker_client.py; unittest discovery alone would not require it.
"""

from tools.container_guard import require_container

require_container()

import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_baseline_launcher as launcher_fixture


class BaselineAdmissionTests(unittest.TestCase):
    def setUp(self):
        launcher_fixture.BaselineRunLauncherTests.setUp(self)
        self.binary = self.root / "bin"
        self.binary.mkdir()
        for name in ("docker", "rm"):
            (self.binary / name).symlink_to(Path(__file__).resolve())
        self.calls = self.root / "calls.jsonl"
        self.observation = self.root / "observation.json"
        self.environment |= {
            "PATH": str(self.binary) + ":" + self.environment["PATH"],
            "PYTHONPATH": str(Path.cwd()),
            "BASELINE_CALLS": str(self.calls),
            "BASELINE_OBSERVATION": str(self.observation),
            "BASELINE_REVISION": self.revision,
            # Ordinary inspection belongs to this fixture, not the ambient
            # environment; per-call fault additions still override its status.
            "BASELINE_INSPECT_STATUS": "0",
            "BASELINE_MUTATION": "",
            "BASELINE_PRIVATE_ERROR": "",
            "BASELINE_RECEIPT_TYPE": "",
            "BASELINE_REMOVE": "",
        }

    def command(self, action, additions):
        return ["/bin/bash", str(self.root / "tools/baseline.sh"), action], self.environment | additions

    # silent-defaults: allow-contract -- Omitted inputs select run admission with a five-second local subprocess observation bound; the consumer checks exact metadata and receipt removal before the inert run; proof=tests/test_baseline_admission.py::BaselineAdmissionTests.test_run_admits_one_exact_identity_and_label_before_dispatch
    def call(self, action="run", *, additions=None, timeout=5):
        command, environment = self.command(action, {} if additions is None else additions)
        result = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=timeout, check=False)
        self.retain_cleanup(result.stderr)
        return result

    def retain_cleanup(self, stderr):
        for name in re.findall(r"/tmp/nmrnet-(?:baseline-admission|inference-native|docker-client)\.[A-Za-z0-9]+", stderr):
            self.addCleanup(subprocess.run, ["/bin/rm", "-rf", "--", name], check=True)

    def engine_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def assert_no_run(self):
        self.assertFalse(any(call["arguments"][0] == "run" for call in self.engine_calls()))
        self.assertFalse((self.root / ".work/baseline/runs").exists())

    def test_saved_record_bytes_reject_before_any_engine_dispatch(self):
        image = self.root / ".work/inference/image-id"
        valid = b"sha256:" + b"a" * 64 + b"\n"
        for data in (
            valid[:-1],
            valid + b"\n",
            valid.replace(b"\n", b"\r\n"),
            valid.replace(b"a", b"\0", 1),
            valid + valid,
            valid.upper(),
        ):
            with self.subTest(data=data):
                image.write_bytes(data)
                result = self.call()
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertEqual([], self.engine_calls())
                self.assert_no_run()

    def test_run_admits_one_exact_identity_and_label_before_dispatch(self):
        result = self.call()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("scientific-fixture\n", result.stdout)
        calls = self.engine_calls()
        self.assertEqual(["image", "run"], [call["arguments"][0] for call in calls])
        inspect = calls[0]["arguments"]
        self.assertEqual("sha256:" + "a" * 64, inspect[-1])
        self.assertIn("{{.Id}}|", inspect[3])
        self.assertIn(inspect[-1], calls[1]["arguments"])
        observed = json.loads(self.observation.read_text())
        self.assertEqual(0o700, observed["directory_mode"])
        self.assertEqual([0o600, 0o600], observed["receipt_modes"])
        self.assertFalse(Path(observed["directory"]).exists())
        self.assertTrue(calls[1]["admission_removed"])

    def test_prepare_admits_only_observed_image_id(self):
        subprocess.run(["/bin/rm", "-rf", "--", str(self.root / ".work/baseline")], check=True)
        result = self.call("prepare")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("{{.Id}}", self.engine_calls()[0]["arguments"][3])

    def test_observed_metadata_byte_mutations_and_identity_mismatch_block_run(self):
        for mutation in ("nul", "cr", "missing-lf", "extra-lf", "duplicate", "wrong-id", "wrong-label"):
            with self.subTest(mutation=mutation):
                self.calls.unlink(missing_ok=True)
                result = self.call(additions={"BASELINE_MUTATION": mutation})
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertEqual("", result.stdout)
                self.assert_no_run()

    def test_failed_inspect_preserves_status_and_private_receipt(self):
        for status in (7, 124, 137, 130, 143):
            with self.subTest(status=status):
                self.calls.unlink(missing_ok=True)
                result = self.call(additions={"BASELINE_INSPECT_STATUS": str(status), "BASELINE_PRIVATE_ERROR": "private-canary"})
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertNotIn("private-canary", result.stdout + result.stderr)
                self.assert_no_run()
                observed = json.loads(self.observation.read_text())
                self.assertTrue(Path(observed["directory"]).is_dir())
                self.assertIn(b"private-canary", (Path(observed["directory"]) / "stderr").read_bytes())

    def test_inspection_status_is_owned_and_missing_status_cannot_admit_run(self):
        with patch.dict(os.environ, {"BASELINE_INSPECT_STATUS": "37"}):
            self.setUp()
        result = self.call()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("scientific-fixture\n", result.stdout)
        self.assertEqual(["image", "run"], [call["arguments"][0] for call in self.engine_calls()])

        self.setUp()
        del self.environment["BASELINE_INSPECT_STATUS"]
        result = self.call()
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        self.assertEqual(["image"], [call["arguments"][0] for call in self.engine_calls()])
        self.assert_no_run()
        observed = json.loads(self.observation.read_text())
        directory = Path(observed["directory"])
        self.assertTrue(directory.is_dir())
        self.assertEqual(("sha256:" + "a" * 64 + "|" + self.revision + "\n").encode(), (directory / "stdout").read_bytes())
        self.assertIn(b"KeyError: 'BASELINE_INSPECT_STATUS'", (directory / "stderr").read_bytes())
        self.assertNotIn("KeyError", result.stderr)

    def test_inspection_selectors_are_owned_despite_ambient_fault_inputs(self):
        private = "ambient-inspection-private-canary"
        with patch.dict(
            os.environ,
            {"BASELINE_MUTATION": "wrong-id", "BASELINE_PRIVATE_ERROR": private, "BASELINE_RECEIPT_TYPE": "stdout-fifo"},
        ):
            self.setUp()
        expected = ("sha256:" + "a" * 64 + "|" + self.revision + "\n").encode()
        result = self.call(additions={"BASELINE_INSPECT_STATUS": "7"})
        self.assertEqual(7, result.returncode, msg=result.stderr)
        self.assertEqual("", result.stdout)
        self.assertNotIn(private, result.stdout + result.stderr)
        self.assertEqual(["image"], [call["arguments"][0] for call in self.engine_calls()])
        self.assert_no_run()
        directory = Path(json.loads(self.observation.read_text())["directory"])
        self.assertTrue(directory.is_dir())
        for name in ("stdout", "stderr"):
            self.assertTrue((directory / name).is_file())
            self.assertFalse((directory / name).is_symlink())
        self.assertEqual(expected, (directory / "stdout").read_bytes())
        self.assertEqual(b"", (directory / "stderr").read_bytes())

    def test_missing_inspection_selectors_preserve_read_position_effects(self):
        expected = ("sha256:" + "a" * 64 + "|" + self.revision + "\n").encode()
        for field, expected_stdout in (
            ("BASELINE_MUTATION", b""),
            ("BASELINE_PRIVATE_ERROR", expected),
            ("BASELINE_RECEIPT_TYPE", expected),
        ):
            with self.subTest(field=field):
                self.setUp()
                del self.environment[field]
                result = self.call()
                self.assertEqual(1, result.returncode, msg=result.stderr)
                self.assertEqual("", result.stdout)
                self.assertEqual(["image"], [call["arguments"][0] for call in self.engine_calls()])
                self.assert_no_run()
                directory = Path(json.loads(self.observation.read_text())["directory"])
                self.assertTrue(directory.is_dir())
                self.assertEqual(expected_stdout, (directory / "stdout").read_bytes())
                self.assertIn(f"KeyError: '{field}'".encode(), (directory / "stderr").read_bytes())
                self.assertNotIn("KeyError", result.stderr)

    def test_successful_stderr_is_private_and_separate_from_identity(self):
        result = self.call(additions={"BASELINE_PRIVATE_ERROR": "private-success-warning"})
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertNotIn("private-success-warning", result.stdout + result.stderr)

    def test_either_full_receipt_blocks_even_zero_status_inspect(self):
        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                self.calls.unlink(missing_ok=True)
                result = self.call(additions={"BASELINE_FULL_RECEIPT": stream})
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertIn("4096-byte limit", result.stderr)
                self.assert_no_run()
                observed = json.loads(self.observation.read_text())
                self.assertEqual(4096, (Path(observed["directory"]) / stream).stat().st_size)
                if stream == "stderr":
                    self.assertEqual(113, (Path(observed["directory"]) / "stdout").stat().st_size)

    def test_nonregular_or_link_receipts_reject_without_blocking(self):
        for mutation in (
            "stdout-fifo",
            "stdout-directory",
            "stdout-symlink",
            "stderr-fifo",
            "stderr-directory",
            "stderr-symlink",
        ):
            with self.subTest(mutation=mutation):
                self.calls.unlink(missing_ok=True)
                result = self.call(additions={"BASELINE_RECEIPT_TYPE": mutation})
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertIn("regular files without symlinks", result.stderr)
                self.assert_no_run()

    def test_removal_selector_is_owned_despite_ambient_failure(self):
        with patch.dict(os.environ, {"BASELINE_REMOVE": "partial"}):
            self.setUp()
        result = self.call()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("scientific-fixture\n", result.stdout)
        self.assertEqual("", result.stderr)
        calls = self.engine_calls()
        self.assertEqual(["image", "run"], [call["arguments"][0] for call in calls])
        observed = json.loads(self.observation.read_text())
        self.assertFalse(Path(observed["directory"]).exists())
        self.assertTrue(calls[1]["admission_removed"])
        self.assertFalse(Path(calls[0]["config"]).parent.exists())

    def test_missing_removal_selector_preserves_receipts_and_inspection_failure_precedence(self):
        private = "removal-selector-private-inspection-cause"
        for inspect_status, expected in ((0, 1), (7, 7)):
            with self.subTest(inspect_status=inspect_status):
                self.setUp()
                del self.environment["BASELINE_REMOVE"]
                result = self.call(additions={"BASELINE_INSPECT_STATUS": str(inspect_status), "BASELINE_PRIVATE_ERROR": private})
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assertEqual("", result.stdout)
                self.assertEqual(["image"], [call["arguments"][0] for call in self.engine_calls()])
                self.assert_no_run()
                directory = Path(json.loads(self.observation.read_text())["directory"])
                self.assertTrue(directory.is_dir())
                self.assertFalse(directory.is_symlink())
                self.assertEqual(0o700, stat.S_IMODE(directory.stat().st_mode))
                self.assertEqual(
                    ("sha256:" + "a" * 64 + "|" + self.revision + "\n").encode(), (directory / "stdout").read_bytes()
                )
                self.assertEqual(private.encode(), (directory / "stderr").read_bytes())
                self.assertNotIn(private, result.stdout + result.stderr)
                self.assertFalse(Path(self.engine_calls()[0]["config"]).parent.exists())
                if inspect_status == 0:
                    self.assertIn("KeyError: 'BASELINE_REMOVE'", result.stderr)
                    self.assertIn("Cannot confirm baseline image admission scratch removal; attempted path", result.stderr)
                    self.assertIn("(status 1)", result.stderr)
                    self.assertNotIn("retained", result.stderr)
                else:
                    self.assertNotIn("KeyError", result.stderr)
                    self.assertIn("private receipts retained at", result.stderr)

    def test_failed_or_partial_removal_does_not_claim_retained_directory(self):
        for mode in ("fail", "partial"):
            with self.subTest(mode=mode):
                self.calls.unlink(missing_ok=True)
                result = self.call(additions={"BASELINE_REMOVE": mode})
                self.assertEqual(23, result.returncode, msg=result.stderr)
                self.assertIn("Cannot confirm", result.stderr)
                self.assertNotIn("retained", result.stderr)
                self.assert_no_run()
                observed = json.loads(self.observation.read_text())
                self.assertEqual(mode == "fail", Path(observed["directory"]).exists())

    def test_interrupted_removal_preserves_cancellation_and_effect_uncertainty(self):
        for mode in ("interrupt-before", "interrupt-after"):
            with self.subTest(mode=mode):
                self.calls.unlink(missing_ok=True)
                result = self.call(additions={"BASELINE_REMOVE": mode})
                self.assertEqual(143, result.returncode, msg=result.stderr)
                self.assertEqual("", result.stderr)
                self.assertNotIn("retained", result.stderr)
                self.assert_no_run()
                observed = json.loads(self.observation.read_text())
                self.assertEqual(mode == "interrupt-before", Path(observed["directory"]).exists())

    def test_hup_int_term_during_inspection_retain_receipts_after_native_retirement(self):
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.calls.unlink(missing_ok=True)
                self.observation.unlink(missing_ok=True)
                command, environment = self.command("run", {"BASELINE_HOLD": "1"})
                process = subprocess.Popen(
                    command, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                )
                self.addCleanup(self.retire_probe, process)
                deadline = time.monotonic() + 3
                while not self.observation.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(self.observation.exists())
                observed = json.loads(self.observation.read_text())
                process.send_signal(signum)
                stdout, stderr = process.communicate(timeout=4)
                self.retain_cleanup(stderr)
                self.assertEqual(128 + signum, process.returncode, msg=stderr)
                self.assertEqual("", stdout)
                self.assertTrue(Path(observed["directory"]).is_dir())
                self.assertFalse(Path("/proc", str(observed["pid"])).exists())
                self.assert_no_run()

    def test_hup_int_term_after_removal_before_handoff_never_dispatch_scientific_work(self):
        """Delay the private caller copy at the previously unprotected handoff."""
        caller = self.root / "tools/baseline.sh"
        source = caller.read_text()
        handoff = '    if [[ $baseline_cancelled != 0 ]]; then exit "$baseline_cancelled"; fi\n'
        self.assertEqual(1, source.count(handoff))
        delayed = handoff + (
            '    printf "%s\\n" "$admission_directory" > "$BASELINE_HANDOFF.pending"\n'
            '    /bin/mv -- "$BASELINE_HANDOFF.pending" "$BASELINE_HANDOFF"\n'
            # Every case requires cancellation here; there is no timed release
            # that can dispatch work while the observer is descheduled.
            "    while :; do sleep 0.01; done\n"
        )
        caller.write_text(source.replace(handoff, delayed, 1))
        receipt = self.root / "handoff"
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.calls.unlink(missing_ok=True)
                receipt.unlink(missing_ok=True)
                command, environment = self.command("run", {"BASELINE_HANDOFF": str(receipt)})
                process = subprocess.Popen(
                    command, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                )
                try:
                    deadline = time.monotonic() + 3
                    while not receipt.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(receipt.is_file(), msg="caller did not reach the post-removal handoff")
                    self.assertFalse(receipt.is_symlink())
                    raw = receipt.read_bytes()
                    self.assertRegex(raw, rb"\A/tmp/nmrnet-baseline-admission\.[A-Za-z0-9]{8}\n\Z")
                    removed_directory = Path(os.fsdecode(raw[:-1]))
                    self.assertEqual(json.loads(self.observation.read_text())["directory"], str(removed_directory))
                    self.assertFalse(removed_directory.exists(), msg="the handoff preceded physical removal")
                    self.assertIsNone(process.poll())
                    process.send_signal(signum)
                    stdout, stderr = process.communicate(timeout=4)
                    self.assertEqual(128 + signum, process.returncode, msg=stderr)
                    self.assertEqual("", stdout)
                    self.assertEqual("", stderr)
                    self.assertNotIn("retained", stderr)
                    self.assert_no_run()
                finally:
                    # End this case before another subtest reuses its calls or
                    # output root, including after a pre-signal assertion fails.
                    self.retire_probe(process)
                    process.communicate(timeout=3)

    def retire_probe(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)

    def test_actual_thirty_second_metadata_deadline_blocks_scientific_dispatch(self):
        result = self.call(additions={"BASELINE_HOLD": "1"}, timeout=35)
        self.assertEqual(124, result.returncode, msg=result.stderr)
        self.assertIn("deadline elapsed", result.stderr)
        self.assert_no_run()
        observed = json.loads(self.observation.read_text())
        self.assertFalse(Path("/proc", str(observed["pid"])).exists())

    def test_print_has_no_native_or_scratch_effect(self):
        result = launcher_fixture.BaselineRunLauncherTests.command(self)
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual([], self.engine_calls())
        self.assertFalse(self.observation.exists())
        self.assertFalse((self.root / ".work/baseline/runs").exists())


def fake_docker():
    """Model only this launcher's inspected metadata and final inert run."""
    arguments = sys.argv[1:]
    if len(arguments) < 5 or arguments[0] != "--config" or arguments[2:4] != ["--host", "unix:///var/run/docker.sock"]:
        return 97
    config = Path(arguments[1])
    if (
        not config.is_dir()
        or config.is_symlink()
        or config.name != "config"
        or not config.parent.name.startswith("nmrnet-docker-client.")
        or config.parent.is_symlink()
        or stat.S_IMODE(config.parent.stat().st_mode) != 0o700
        or stat.S_IMODE(config.stat().st_mode) != 0o700
        or list(config.iterdir())
    ):
        return 97
    if "BASELINE_SCRUB_NAMES" in os.environ:
        if any(name in os.environ for name in os.environ["BASELINE_SCRUB_NAMES"].split(",")):
            return 97
        if os.environ.get("DOCKER_BUILDKIT") != "1":
            return 97
    arguments = arguments[4:]
    calls = Path(os.environ["BASELINE_CALLS"])
    observation = os.environ.get("BASELINE_OBSERVATION")
    record = {"arguments": arguments, "config": str(config)}
    if arguments[:1] == ["run"]:
        if observation:
            directory = json.loads(Path(observation).read_text())["directory"]
            record["admission_removed"] = not Path(directory).exists()
        with calls.open("a") as log:
            log.write(json.dumps(record) + "\n")
        if "BASELINE_RUN_BEHAVIOR" in os.environ:
            return scientific_fixture()
        sys.stdout.write("scientific-fixture\n")
        return 0
    if len(arguments) != 5 or arguments[:3] != ["image", "inspect", "--format"]:
        return 97
    if arguments[3] not in ("{{.Id}}", '{{.Id}}|{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}'):
        return 97
    with calls.open("a") as log:
        log.write(json.dumps(record) + "\n")
    directory = Path("/proc/self/fd/1").readlink().parent
    if observation:
        receipt = Path(observation)
        receipt.with_suffix(".pending").write_text(
            json.dumps(
                {
                    "directory": str(directory),
                    "pid": os.getpid(),
                    "anchor": os.getpgrp(),
                    "directory_mode": stat.S_IMODE(directory.stat().st_mode),
                    "receipt_modes": [stat.S_IMODE((directory / name).stat().st_mode) for name in ("stdout", "stderr")],
                }
            )
        )
        receipt.with_suffix(".pending").replace(receipt)
    value = arguments[-1]
    if arguments[3] != "{{.Id}}":
        value += "|" + os.environ["BASELINE_REVISION"]
    data = value.encode() + b"\n"
    mutation = os.environ["BASELINE_MUTATION"]
    if mutation == "nul":
        data = data.replace(b"a", b"\0", 1)
    elif mutation == "cr":
        data = data[:-1] + b"\r\n"
    elif mutation == "missing-lf":
        data = data[:-1]
    elif mutation == "extra-lf":
        data += b"\n"
    elif mutation == "duplicate":
        data += data
    elif mutation == "wrong-id":
        data = data.replace(b"a", b"c")
    elif mutation == "wrong-label":
        data = data.replace(b"b", b"c")
    if os.environ.get("BASELINE_FULL_RECEIPT") == "stdout":
        data = b"x" * 4096
    os.write(1, data)
    diagnostic = os.environ["BASELINE_PRIVATE_ERROR"].encode()
    if os.environ.get("BASELINE_FULL_RECEIPT") == "stderr":
        diagnostic = b"x" * 4096
    os.write(2, diagnostic)
    receipt_type = os.environ["BASELINE_RECEIPT_TYPE"]
    if receipt_type:
        stream, kind = receipt_type.split("-")
        target = directory / stream
        target.unlink()
        if kind == "fifo":
            os.mkfifo(target)
        elif kind == "directory":
            target.mkdir()
        else:
            target.symlink_to(directory)
    if os.environ.get("BASELINE_HOLD") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(40)
    return int(os.environ["BASELINE_INSPECT_STATUS"])


def scientific_fixture():
    """Exercise this caller's native streams/lifetime, never scientific modules."""
    behavior = os.environ["BASELINE_RUN_BEHAVIOR"]
    observation = Path(os.environ["BASELINE_RUN_OBSERVATION"])
    observed = {"pid": os.getpid(), "anchor": os.getpgrp(), "config": sys.argv[2]}
    if behavior in {"live", "blocked"}:
        child = subprocess.Popen(["/bin/bash", "-c", "trap '' HUP INT TERM; exec sleep 60"])
        observed["descendant"] = child.pid
    observation.with_suffix(".pending").write_text(json.dumps(observed))
    observation.with_suffix(".pending").replace(observation)
    if behavior in {"live", "finite"}:
        os.write(1, b"stdout\0\r\n")
        os.write(2, b"stderr\0\r\n")
    if behavior == "live":
        time.sleep(60)
    elif behavior == "blocked":
        stream = os.environ["BASELINE_BLOCK_STREAM"]
        if stream == "stdout":
            sys.stdout.buffer.write(b"x" * 8388609)
            sys.stdout.buffer.flush()
        elif stream == "stderr":
            sys.stderr.buffer.write(b"x" * 8388609)
            sys.stderr.buffer.flush()
        else:
            raise ValueError("Blocked baseline fixture requires explicit stdout or stderr")
    elif behavior != "finite":
        raise ValueError("Baseline scientific fixture requires finite, live or blocked")
    return int(os.environ["BASELINE_RUN_STATUS"])


def fake_remove():
    """Expose removal's partial effects without claiming a retained path."""
    arguments = sys.argv[1:]
    target = arguments[-1]
    if target.startswith("/tmp/nmrnet-docker-client."):
        mode = os.environ.get("BASELINE_CONFIG_REMOVE")
        if mode == "fail":
            return 23
        if mode == "after-delete":
            subprocess.run(["/bin/rm", *arguments], check=True)
            sys.stderr.write("fixture config cleanup failed after deletion\n")
            return 23
    if target.startswith("/tmp/nmrnet-baseline-admission."):
        mode = os.environ["BASELINE_REMOVE"]
        if mode == "fail":
            return 23
        if mode in {"partial", "interrupt-after"}:
            subprocess.run(["/bin/rm", *arguments], check=True)
        if mode == "partial":
            return 23
        if mode in {"interrupt-before", "interrupt-after"}:
            os.kill(os.getppid(), signal.SIGTERM)
            time.sleep(0.15)
            return 0
    os.execv("/bin/rm", ["rm", *arguments])
    return 97


if __name__ == "__main__":
    command_name = Path(sys.argv[0]).name
    if command_name == "docker":
        raise SystemExit(fake_docker())
    if command_name == "rm":
        raise SystemExit(fake_remove())
    unittest.main()
