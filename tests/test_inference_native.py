#!/usr/bin/env python3
"""Probe captured client ownership using finite offline process and file faults."""

from tools.container_guard import require_container

require_container()

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


class CapturedNativeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.helper = self.root / "inference_native.sh"
        self.helper.write_bytes(Path("tools/inference_native.sh").read_bytes())
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("mv", "head", "rm"):
            (self.bin / name).symlink_to(Path(__file__).resolve())
        self.observation = self.root / "client.json"
        self.control = self.root / "control.json"
        self.environment = os.environ | {
            "PATH": str(self.bin) + ":" + os.environ["PATH"],
            "PYTHONPATH": str(Path.cwd()),
            "NATIVE_OBSERVATION": str(self.observation),
            "NATIVE_CONTROL": str(self.control),
        }

    def command(self, *, budget="20", environment=None):
        return [
            "/bin/bash",
            "-euc",
            'source "$1"; inference_native probe "$2" discard "$3" "$4" native-client',
            "bash",
            str(self.helper),
            budget,
            sys.executable,
            str(Path(__file__).resolve()),
        ], (self.environment if environment is None else environment)

    def run_native(self, *, budget="20", additions=None):
        command, environment = self.command(budget=budget)
        result = subprocess.run(
            command,
            env=environment | ({} if additions is None else additions),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.retain_cleanup(result.stderr)
        return result

    def retain_cleanup(self, stderr):
        for name in re.findall(r"/tmp/nmrnet-inference-native\.[A-Za-z0-9]+", stderr):
            self.addCleanup(subprocess.run, ["/bin/rm", "-rf", "--", name], check=True)

    def assert_retired(self, identifier):
        process = Path("/proc", str(identifier))
        deadline = time.monotonic() + 2
        while process.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(process.exists(), msg=f"owned fixture process {identifier} remained")

    def assert_client_retired(self):
        observed = json.loads(self.observation.read_text())
        self.assert_retired(observed["pid"])
        if "descendant" in observed:
            self.assert_retired(observed["descendant"])

    def test_real_native_137_is_distinct_from_confirmed_deadline(self):
        result = self.run_native(additions={"NATIVE_STATUS": "137"})
        self.assertEqual(137, result.returncode, msg=result.stderr)
        self.assertIn("native client exited with status 137", result.stderr)
        self.assertNotIn("deadline elapsed", result.stderr)
        self.assert_client_retired()

    def test_resistant_expiry_forwards_term_and_retires_the_full_group(self):
        result = self.run_native(budget="0.1", additions={"NATIVE_BEHAVIOR": "resistant"})
        self.assertEqual(124, result.returncode, msg=result.stderr)
        self.assertIn("deadline elapsed", result.stderr)
        self.assertTrue((self.root / "term-observed").exists(), msg="the actual client never received deadline TERM")
        self.assert_client_retired()

    def test_late_success_after_confirmed_expiry_cannot_report_success(self):
        result = self.run_native(budget="0.1", additions={"NATIVE_BEHAVIOR": "late-success"})
        self.assertEqual(124, result.returncode, msg=result.stderr)
        self.assertIn("deadline elapsed", result.stderr)
        self.assert_client_retired()

    def test_cli_signal_dispositions_are_native(self):
        result = self.run_native()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        observed = json.loads(self.observation.read_text())
        self.assertFalse(observed["sigint_ignored"])
        self.assertFalse(observed["sigterm_ignored"])

    def test_output_eof_is_not_completion(self):
        started = time.monotonic()
        result = self.run_native(additions={"NATIVE_BEHAVIOR": "closed-output", "NATIVE_STATUS": "7"})
        self.assertEqual(7, result.returncode, msg=result.stderr)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assert_client_retired()

    def test_signal_cancellation_preserves_status_and_retires_resistant_descendant(self):
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.observation.unlink(missing_ok=True)
                command, environment = self.command()
                process = subprocess.Popen(
                    command,
                    env=environment | {"NATIVE_BEHAVIOR": "resistant"},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                self.addCleanup(self.retire_probe, process)
                self.await_file(self.observation)
                process.send_signal(signum)
                stdout, stderr = process.communicate(timeout=4)
                self.retain_cleanup(stderr)
                self.assertEqual(128 + signum, process.returncode, msg=stderr)
                self.assertEqual("", stdout)
                self.assert_client_retired()

    def await_file(self, path):
        deadline = time.monotonic() + 3
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(path.exists(), msg=f"fixture did not publish {path.name}")

    def retire_probe(self, process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=3)

    def test_birth_cancellation_never_releases_native_dispatch(self):
        source = self.helper.read_text()
        capture = "    leader=$!\n"
        self.assertEqual(1, source.count(capture))
        delayed = '    printf "%s\\n" "$!" "$scratch" > "$NATIVE_BIRTH"\n    sleep 0.25\n' + capture
        self.helper.write_text(source.replace(capture, delayed, 1))
        birth = self.root / "birth"
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                birth.unlink(missing_ok=True)
                command, environment = self.command()
                process = subprocess.Popen(
                    command,
                    env=environment | {"NATIVE_BIRTH": str(birth)},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                self.addCleanup(self.retire_probe, process)
                self.await_file(birth)
                anchor, _ = birth.read_text().splitlines()
                process.send_signal(signum)
                _, stderr = process.communicate(timeout=4)
                self.retain_cleanup(stderr)
                self.assertEqual(128 + signum, process.returncode, msg=stderr)
                self.assert_retired(int(anchor))
                self.assertFalse(self.observation.exists(), msg="cancelled birth dispatched a native client")

    def test_failed_startup_with_no_writer_joins_the_drainer(self):
        source = self.helper.read_text()
        launch = '        exec bash -c "$anchor_program" bash "$scratch/status" "$owning_parent" "$budget" "$timed_program" "$@"'
        self.assertEqual(1, source.count(launch))
        source = source.replace(launch, "        exit 28", 1)
        source = source.replace('    ) > "$scratch/output" 2>&1 &', "    ) > /dev/null 2>&1 &", 1)
        source = source.replace("    drain=$!\n", '    drain=$!\n    printf "%s\\n" "$drain" > "$NATIVE_DRAIN"\n', 1)
        self.helper.write_text(source)
        receipt = self.root / "drain"
        result = self.run_native(additions={"NATIVE_DRAIN": str(receipt)})
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("native anchor did not publish", result.stderr)
        self.assert_retired(int(receipt.read_text()))
        self.assertFalse(self.observation.exists())

    def test_ready_or_release_publication_failure_blocks_dispatch(self):
        for phase, expected in (
            ("ready", 1),
            ("release", 29),
            ("release-fifo", 1),
            ("release-directory", 1),
            ("release-symlink", 1),
        ):
            with self.subTest(phase=phase):
                result = self.run_native(additions={"NATIVE_PUBLICATION_FAILURE": phase})
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assertFalse(self.observation.exists())
                self.assert_retired(json.loads(self.control.read_text())["anchor"])

    def test_corrupt_ready_receipt_retains_uncertain_state_without_dispatch(self):
        receipt = self.root / "drain"
        source = self.helper.read_text().replace(
            "    drain=$!\n",
            '    drain=$!\n    printf "%s\\n" "$drain" > "$NATIVE_DRAIN"\n',
            1,
        )
        self.helper.write_text(source)
        result = self.run_native(additions={"NATIVE_CORRUPT_READY": "1", "NATIVE_DRAIN": str(receipt)})
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("Cannot confirm inference client retirement", result.stderr)
        self.assertFalse(self.observation.exists())
        observed = json.loads(self.control.read_text())
        self.assertTrue(Path(observed["directory"]).is_dir())
        # This external probe owns the finite injected anchor and retires it;
        # production correctly made no group effect from its corrupted receipt.
        os.killpg(observed["anchor"], signal.SIGKILL)
        self.assert_retired(observed["anchor"])
        self.assert_retired(int(receipt.read_text()))

    def test_terminal_publication_failure_is_prompt_and_retires_held_descendant(self):
        for phase in ("status", "printf"):
            with self.subTest(phase=phase):
                if phase == "printf":
                    source = self.helper.read_text()
                    terminal = '        if printf "%s\\n" "$status" > "$receipt.pending" \\\n'
                    self.assertEqual(1, source.count(terminal))
                    self.helper.write_text(source.replace(terminal, "        if false \\\n", 1))
                started = time.monotonic()
                result = self.run_native(additions={"NATIVE_PUBLICATION_FAILURE": phase, "NATIVE_BEHAVIOR": "held-descendant"})
                self.assertEqual(1, result.returncode, msg=result.stderr)
                self.assertLess(time.monotonic() - started, 3)
                self.assertIn("publication failed; the client status is unobserved", result.stderr)
                self.assert_client_retired()

    def test_status_read_and_byte_mutations_retire_before_cleanup(self):
        for mutation, expected in (
            ("read-error", 1),
            ("symlink", 1),
            ("fifo", 1),
            ("directory", 1),
            ("deadline-fifo", 1),
            ("deadline-directory", 1),
            ("deadline-symlink", 1),
            ("nul", 2),
            ("cr", 2),
            ("missing-lf", 2),
            ("extra-lf", 2),
            ("extra-line", 2),
            ("negative", 2),
            ("256", 2),
            ("text", 2),
        ):
            with self.subTest(mutation=mutation):
                result = self.run_native(additions={"NATIVE_STATUS_MUTATION": mutation, "NATIVE_BEHAVIOR": "held-descendant"})
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                if mutation.startswith("deadline-"):
                    self.assertIn("native deadline receipt", result.stderr)
                else:
                    self.assertIn("native completion receipt", result.stderr)
                self.assert_client_retired()

    def test_failed_expiry_publication_cannot_claim_a_confirmed_deadline(self):
        result = self.run_native(
            budget="0.1", additions={"NATIVE_PUBLICATION_FAILURE": "deadline", "NATIVE_BEHAVIOR": "resistant"}
        )
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertIn("publication failed", result.stderr)
        self.assertNotIn("deadline elapsed", result.stderr)
        self.assert_client_retired()

    def test_drain_failure_preserves_native_failure_and_rejects_success(self):
        for native_status, expected in (("0", 33), ("7", 7)):
            with self.subTest(status=native_status):
                result = self.run_native(additions={"NATIVE_STATUS": native_status, "NATIVE_DRAIN_FAILURE": "1"})
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assert_client_retired()

    def test_cancellation_wins_over_terminal_publication_failure(self):
        result = self.run_native(
            additions={
                "NATIVE_PUBLICATION_FAILURE": "status",
                "NATIVE_CANCEL_PUBLICATION": "1",
                "NATIVE_BEHAVIOR": "held-descendant",
            }
        )
        self.assertEqual(143, result.returncode, msg=result.stderr)
        self.assertIn("interrupted", result.stderr)
        self.assert_client_retired()

    def test_cleanup_preserves_primary_and_traps_are_restored(self):
        for native_status, expected in (("0", 23), ("7", 7)):
            with self.subTest(status=native_status):
                result = self.run_native(additions={"NATIVE_STATUS": native_status, "NATIVE_CLEANUP_FAILURE": "1"})
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assertIn("Cannot remove inference client", result.stderr)
        command, environment = self.command()
        command[2] = (
            'trap ":" HUP INT TERM USR1; before=$(trap -p HUP INT TERM USR1); source "$1"; '
            'inference_native probe "$2" discard true; [[ $(trap -p HUP INT TERM USR1) == "$before" ]]'
        )
        result = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=5, check=False)
        self.assertEqual(0, result.returncode, msg=result.stderr)


def fixture_command(name):
    """Inject only named local file faults; all other operations use native tools."""
    arguments = sys.argv[1:]
    phase = os.environ.get("NATIVE_PUBLICATION_FAILURE", "")
    if name == "mv":
        source, target = map(Path, arguments[-2:])
        receipt = target.parent / "status.ready"
        if target.name == "status.ready":
            anchor = int(source.read_text())
        elif receipt.exists():
            anchor = int(receipt.read_text())
        else:
            anchor = 0
        Path(os.environ["NATIVE_CONTROL"]).write_text(json.dumps({"anchor": anchor, "directory": str(target.parent)}))
        if target.name == "status.ready" and os.environ.get("NATIVE_CORRUPT_READY") == "1":
            source.write_bytes(b"not-the-anchor\n")
        if target.name == "status.release" and phase.startswith("release-"):
            if phase == "release-fifo":
                os.mkfifo(target)
            elif phase == "release-directory":
                target.mkdir()
            else:
                target.symlink_to(source)
            return 0
        if target.name == "status." + phase or (target.name == "status" and phase == "status"):
            if os.environ.get("NATIVE_CANCEL_PUBLICATION") == "1":
                parent = int(Path("/proc", str(anchor), "stat").read_text().split()[3])
                os.kill(parent, signal.SIGTERM)
            return 29
        if target.name == "status":
            mutation = os.environ.get("NATIVE_STATUS_MUTATION", "")
            values = {
                "nul": b"0\0\n",
                "cr": b"0\r\n",
                "missing-lf": b"0",
                "extra-lf": b"0\n\n",
                "extra-line": b"0\n7\n",
                "negative": b"-1\n",
                "256": b"256\n",
                "text": b"unknown\n",
            }
            if mutation in values:
                source.write_bytes(values[mutation])
            if mutation == "symlink":
                target.symlink_to(source)
                return 0
            if mutation in {"fifo", "directory", "deadline-fifo", "deadline-directory", "deadline-symlink"}:
                if mutation.startswith("deadline-"):
                    target = target.parent / "status.deadline"
                if mutation.endswith("fifo"):
                    os.mkfifo(target)
                elif mutation.endswith("directory"):
                    target.mkdir()
                else:
                    target.symlink_to(source)
                return 0
    if name == "head" and arguments[-1].endswith("/status") and os.environ.get("NATIVE_STATUS_MUTATION") == "read-error":
        return 31
    if name == "head" and arguments == ["-c", "8388608"] and os.environ.get("NATIVE_DRAIN_FAILURE") == "1":
        return 33
    if name == "rm" and os.environ.get("NATIVE_CLEANUP_FAILURE") == "1":
        return 23
    os.execv("/bin/" + name, [name, *arguments])
    return 97


def native_client():
    """Model a finite native status, resistant group member or output-only EOF."""
    observation = Path(os.environ["NATIVE_OBSERVATION"])
    behavior = os.environ.get("NATIVE_BEHAVIOR", "")
    observed = {
        "pid": os.getpid(),
        "sigint_ignored": signal.getsignal(signal.SIGINT) == signal.SIG_IGN,
        "sigterm_ignored": signal.getsignal(signal.SIGTERM) == signal.SIG_IGN,
    }
    if behavior in {"resistant", "held-descendant"}:
        descendant = subprocess.Popen(["/bin/bash", "-c", "trap '' HUP INT TERM; exec sleep 60"])
        observed["descendant"] = descendant.pid
    observation.write_text(json.dumps(observed))
    if behavior == "resistant":
        signal.signal(signal.SIGTERM, lambda *_: (observation.parent / "term-observed").write_text("TERM\n"))
        time.sleep(60)
    if behavior == "late-success":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(0.3)
    if behavior == "closed-output":
        os.close(1)
        os.close(2)
        time.sleep(0.25)
    return int(os.environ.get("NATIVE_STATUS", "0"))


if __name__ == "__main__":
    command_name = Path(sys.argv[0]).name
    if command_name in {"mv", "head", "rm"}:
        raise SystemExit(fixture_command(command_name))
    if sys.argv[1:] == ["native-client"]:
        raise SystemExit(native_client())
    unittest.main()
