#!/usr/bin/env python3
"""Probe captured client ownership using finite offline process and file faults."""

from tools.container_guard import require_container

require_container()

import json
import os
import re
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress
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

    def fact_command(self, *, budget, additions=None):
        command, environment = self.command(budget=budget)
        command[2] = (
            'source "$1"; inference_retirement_confirmed=false; '
            'trap \'printf "%s\\n" "$inference_retirement_confirmed" > "$NATIVE_FACT"\' EXIT; '
            'if inference_native probe "$2" discard "$3" "$4" native-client; then status=0; else status=$?; fi; exit "$status"'
        )
        environment = environment | {"NATIVE_FACT": str(self.root / "retired")}
        if additions is not None:
            environment |= additions
        return command, environment

    def run_fact(self, *, budget, additions=None):
        command, environment = self.fact_command(budget=budget, additions=additions)
        result = subprocess.run(command, env=environment, text=True, capture_output=True, timeout=5, check=False)
        self.retain_cleanup(result.stderr)
        return result, (self.root / "retired").read_text().strip()

    def test_untimed_completion_preserves_status_and_has_no_short_operation_deadline(self):
        result, fact = self.run_fact(budget="0.2", additions={"NATIVE_BEHAVIOR": "slow-success"})
        self.assertEqual(124, result.returncode, msg=result.stderr)
        self.assertEqual("true", fact)
        for status in (0, 7, 137):
            with self.subTest(status=status):
                result, fact = self.run_fact(
                    budget="untimed", additions={"NATIVE_BEHAVIOR": "slow-success", "NATIVE_STATUS": str(status)}
                )
                self.assertEqual(status, result.returncode, msg=result.stderr)
                self.assertEqual("true", fact)
                self.assertNotIn("deadline elapsed", result.stderr)
                self.assert_client_retired()

    def test_untimed_output_eof_and_held_descendant_retire_before_fact_is_true(self):
        for behavior in ("closed-output", "held-descendant"):
            with self.subTest(behavior=behavior):
                result, fact = self.run_fact(budget="untimed", additions={"NATIVE_BEHAVIOR": behavior, "NATIVE_STATUS": "7"})
                self.assertEqual(7, result.returncode, msg=result.stderr)
                self.assertEqual("true", fact)
                self.assertEqual("", result.stdout)
                self.assert_client_retired()

    def test_untimed_cancellation_preserves_signals_and_confirms_retirement(self):
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.observation.unlink(missing_ok=True)
                command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BEHAVIOR": "resistant"})
                process = subprocess.Popen(
                    command, env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                )
                self.addCleanup(self.retire_probe, process)
                self.await_file(self.observation)
                process.send_signal(signum)
                stdout, stderr = process.communicate(timeout=4)
                self.retain_cleanup(stderr)
                self.assertEqual(128 + signum, process.returncode, msg=stderr)
                self.assertEqual("true\n", (self.root / "retired").read_text())
                self.assertEqual("", stdout)
                self.assert_client_retired()

    def test_retirement_fact_is_false_during_the_birth_handoff(self):
        source = self.helper.read_text()
        birth = "        (\n            exec {capture_guard}>&-\n            umask 077\n"
        self.assertEqual(1, source.count(birth))
        sample = '            printf "%s\\n" "$inference_retirement_confirmed" "$BASHPID" > "$NATIVE_BIRTH_FACT"\n'
        source = source.replace(birth, "        (\n" + sample + birth[len("        (\n") :], 1)
        capture = "    drain=$!\n"
        self.assertEqual(1, source.count(capture))
        receipt = self.root / "birth-fact"
        self.helper.write_text(source.replace(capture, "    sleep 0.25\n" + capture, 1))
        result, fact = self.run_fact(budget="untimed", additions={"NATIVE_BIRTH_FACT": str(receipt)})
        self.assertEqual(0, result.returncode, msg=result.stderr)
        sampled, child = receipt.read_text().splitlines()
        self.assertEqual("false", sampled)
        self.assert_retired(int(child))
        self.assertEqual("true", fact)

    def test_admitted_anchor_loss_retains_caller_resource_and_stops(self):
        drain_receipt = self.root / "drain"
        sentinel = self.root / "caller-resource"
        sentinel.write_text("still owned")
        source = self.helper.read_text()
        capture = "    drain=$!\n"
        self.assertEqual(1, source.count(capture))
        self.helper.write_text(source.replace(capture, capture + '    printf "%s\\n" "$drain" > "$NATIVE_DRAIN"\n', 1))
        command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BEHAVIOR": "resistant"})
        command[2] = (
            'source "$1"; inference_retirement_confirmed=false; '
            'trap \'if "$inference_retirement_confirmed"; then /bin/rm -f "$NATIVE_SENTINEL"; fi; '
            'printf "%s\\n" "$inference_retirement_confirmed" > "$NATIVE_FACT"\' EXIT; '
            'if inference_native uncertain "$2" discard "$3" "$4" native-client; then status=0; else status=$?; fi; '
            'if ! "$inference_retirement_confirmed"; then exit "$status"; fi; '
            'printf forbidden > "$NATIVE_SUBSEQUENT"; inference_native forbidden 5 discard true'
        )
        process = subprocess.Popen(
            command,
            env=environment
            | {
                "NATIVE_DRAIN": str(drain_receipt),
                "NATIVE_SENTINEL": str(sentinel),
                "NATIVE_SUBSEQUENT": str(self.root / "next"),
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(self.retire_probe, process)
        self.await_file(self.observation)
        observed = json.loads(self.observation.read_text())
        drain = int(drain_receipt.read_text())
        # These finite fixture processes remain recorded after their anchor dies.
        # Cleanup uses only those exact observations, after collecting primary status.
        os.kill(observed["parent"], signal.SIGKILL)
        try:
            stdout, stderr = process.communicate(timeout=4)
            self.retain_cleanup(stderr)
            self.assertEqual(1, process.returncode, msg=stderr)
            self.assertEqual("", stdout)
            self.assertEqual("false\n", (self.root / "retired").read_text())
            self.assertTrue(sentinel.exists())
            self.assertFalse((self.root / "next").exists())
            self.assertIn("Cannot confirm inference client retirement", stderr)
            self.assertTrue(Path("/proc", str(observed["descendant"])).exists())
        finally:
            for identifier in (observed["pid"], observed["descendant"], drain):
                self.kill_recorded_fixture(identifier)

    def kill_recorded_fixture(self, identifier):
        with suppress(ProcessLookupError):
            os.kill(identifier, signal.SIGKILL)
        self.assert_retired(identifier)

    def test_caller_cleanup_reads_confirmed_fact_at_return_signal_handoff(self):
        source = self.helper.read_text()
        handoff = '    if "$monitor_enabled"; then set -m; fi\n    return "$status"\n'
        self.assertEqual(1, source.count(handoff))
        for signum, expected in ((None, 7), (signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
            with self.subTest(signal=signum):
                sentinel = self.root / "caller-resource"
                sentinel.write_text("owned")
                self.helper.write_text(source)
                if signum is not None:
                    self.helper.write_text(source.replace(handoff, f'    kill -{signum.value} "$BASHPID"\n' + handoff, 1))
                command, environment = self.fact_command(budget="untimed", additions={"NATIVE_STATUS": "7"})
                command[2] = (
                    'source "$1"; inference_retirement_confirmed=false; '
                    'trap \'status=$?; trap - EXIT; if "$inference_retirement_confirmed"; then /bin/rm -f "$NATIVE_SENTINEL"; fi; '
                    'printf "%s\\n" "$inference_retirement_confirmed" > "$NATIVE_FACT"; exit "$status"\' EXIT; '
                    "trap 'exit 129' HUP; trap 'exit 130' INT; trap 'exit 143' TERM; "
                    'if inference_native probe "$2" discard "$3" "$4" native-client; then status=0; else status=$?; fi; exit "$status"'
                )
                result = subprocess.run(
                    command,
                    env=environment | {"NATIVE_SENTINEL": str(sentinel)},
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.retain_cleanup(result.stderr)
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assertEqual("true\n", (self.root / "retired").read_text())
                self.assertFalse(sentinel.exists())
                self.assert_client_retired()

    def test_anchor_join_interruption_retries_before_confirming_retirement(self):
        source = self.helper.read_text()
        retirement = 'kill -KILL -- "-$leader"'
        join = '            wait "$leader" 2>/dev/null || :\n'
        self.assertEqual(1, source.count(retirement))
        self.assertEqual(1, source.count(join))
        # TERM leaves the held anchor live so its actual wait can be interrupted.
        # The external probe releases that same held group after observing retry.
        source = source.replace(retirement, 'kill -TERM -- "-$leader"', 1)
        source = source.replace(
            join,
            '            printf "%s %s\\n" "$interrupt_count" "$inference_retirement_confirmed" >> "$NATIVE_JOIN"\n' + join,
            1,
        )
        self.helper.write_text(source)
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.observation.unlink(missing_ok=True)
                receipt = self.root / "join"
                receipt.unlink(missing_ok=True)
                command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BEHAVIOR": "held-descendant"})
                process = subprocess.Popen(
                    command,
                    env=environment | {"NATIVE_JOIN": str(receipt)},
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                self.addCleanup(self.retire_probe, process)
                self.await_file(receipt)
                observed = json.loads(self.observation.read_text())
                try:
                    process.send_signal(signum)
                    deadline = time.monotonic() + 2
                    while len(receipt.read_text().splitlines()) < 2 and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(["0 false", "1 false"], receipt.read_text().splitlines())
                finally:
                    os.killpg(observed["parent"], signal.SIGKILL)
                _, stderr = process.communicate(timeout=4)
                self.retain_cleanup(stderr)
                self.assertEqual(128 + signum, process.returncode, msg=stderr)
                self.assertEqual("true\n", (self.root / "retired").read_text())
                self.assert_client_retired()

    def test_retirement_fact_waits_for_anchor_and_drain_joins(self):
        source = self.helper.read_text()
        join = '            if wait "$drain"; then drain_status=0; else drain_status=$?; fi\n'
        self.assertEqual(1, source.count(join))
        receipt = self.root / "before-join"
        delayed = '            printf "%s\\n" "$inference_retirement_confirmed" > "$NATIVE_JOIN_FACT"\n' + join
        self.helper.write_text(source.replace(join, delayed, 1))
        result, fact = self.run_fact(
            budget="untimed", additions={"NATIVE_JOIN_FACT": str(receipt), "NATIVE_BEHAVIOR": "held-descendant"}
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("false\n", receipt.read_text())
        self.assertEqual("true", fact)
        self.assert_client_retired()

    def test_setup_before_birth_and_post_retirement_cleanup_have_separate_facts(self):
        source = self.helper.read_text()
        creation = "scratch=$(umask 077; mktemp -d /tmp/nmrnet-inference-native.XXXXXXXX)"
        self.assertEqual(1, source.count(creation))
        self.helper.write_text(source.replace(creation, "scratch=$(exit 28)", 1))
        result, fact = self.run_fact(budget="untimed")
        self.assertEqual(28, result.returncode, msg=result.stderr)
        self.assertEqual("true", fact)
        self.assertFalse(self.observation.exists())
        self.helper.write_text(source)
        for status, expected in (("0", 23), ("7", 7)):
            with self.subTest(status=status):
                result, fact = self.run_fact(budget="untimed", additions={"NATIVE_STATUS": status, "NATIVE_CLEANUP_FAILURE": "1"})
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assertEqual("true", fact)
                self.assert_client_retired()

    def test_caller_stops_and_retains_resources_after_unknown_retirement(self):
        sentinel = self.root / "caller-resource"
        sentinel.write_text("still owned")
        command, environment = self.fact_command(budget="untimed", additions={"NATIVE_CORRUPT_READY": "1"})
        command[2] = (
            'source "$1"; inference_retirement_confirmed=false; '
            'trap \'if "$inference_retirement_confirmed"; then /bin/rm -f "$NATIVE_SENTINEL"; fi\' EXIT; '
            "inference_native prior 5 discard true; [[ $inference_retirement_confirmed == true ]]; "
            'if inference_native uncertain "$2" discard "$3" "$4" native-client; then status=0; else status=$?; fi; '
            'if ! "$inference_retirement_confirmed"; then exit "$status"; fi; '
            'printf "%s\\n" forbidden > "$NATIVE_SUBSEQUENT"; inference_native forbidden 5 discard true'
        )
        # Corrupt only the second call: the first true fact cannot grant later cleanup.
        source = self.helper.read_text()
        entry = "    inference_result=\n    inference_retirement_confirmed=true\n"
        self.assertEqual(1, source.count(entry))
        self.helper.write_text(
            source.replace(
                entry,
                entry
                + "    if [[ $operation == prior ]]; then unset NATIVE_CORRUPT_READY; else export NATIVE_CORRUPT_READY=1; fi\n",
                1,
            )
        )
        result = subprocess.run(
            command,
            env=environment | {"NATIVE_SENTINEL": str(sentinel), "NATIVE_SUBSEQUENT": str(self.root / "subsequent")},
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.retain_cleanup(result.stderr)
        self.assertEqual(1, result.returncode, msg=result.stderr)
        self.assertTrue(sentinel.exists())
        self.assertFalse((self.root / "subsequent").exists())
        observed = json.loads(self.control.read_text())
        os.killpg(observed["anchor"], signal.SIGKILL)
        self.assert_retired(observed["anchor"])

    def test_receipt_faults_preserve_status_while_retirement_fact_remains_true(self):
        for additions, expected in (
            ({"NATIVE_PUBLICATION_FAILURE": "status"}, 1),
            ({"NATIVE_STATUS_MUTATION": "read-error"}, 1),
            ({"NATIVE_STATUS_MUTATION": "fifo"}, 1),
            ({"NATIVE_DRAIN_FAILURE": "1"}, 33),
        ):
            with self.subTest(additions=additions):
                result, fact = self.run_fact(budget="untimed", additions=additions | {"NATIVE_BEHAVIOR": "held-descendant"})
                self.assertEqual(expected, result.returncode, msg=result.stderr)
                self.assertEqual("true", fact)
                self.assert_client_retired()

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
        for budget in ("20", "untimed"):
            with self.subTest(budget=budget):
                result = self.run_native(budget=budget)
                self.assertEqual(0, result.returncode, msg=result.stderr)
                observed = json.loads(self.observation.read_text())
                self.assertFalse(observed["sigint_ignored"])
                self.assertFalse(observed["sigterm_ignored"])
                self.assertFalse(observed["sighup_ignored"])

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


class StdioNativeTests(unittest.TestCase):
    # Reuse the existing finite filesystem/process fixture, not captured tests.
    setUp = CapturedNativeTests.setUp
    command = CapturedNativeTests.command
    await_file = CapturedNativeTests.await_file
    assert_retired = CapturedNativeTests.assert_retired
    assert_client_retired = CapturedNativeTests.assert_client_retired
    retain_cleanup = CapturedNativeTests.retain_cleanup
    kill_recorded_fixture = CapturedNativeTests.kill_recorded_fixture
    test_stdio_anchor_join_interruption_retries = (
        CapturedNativeTests.test_anchor_join_interruption_retries_before_confirming_retirement
    )

    def fact_command(self, *, budget, additions):
        command, environment = CapturedNativeTests.fact_command(self, budget=budget, additions=additions)
        self.assertEqual(1, command[2].count(" discard "))
        command[2] = command[2].replace(" discard ", " stdio ", 1)
        return command, environment

    def run_stdio(self, *, budget, additions):
        command, environment = self.fact_command(budget=budget, additions=additions)
        result = subprocess.run(command, env=environment, capture_output=True, timeout=5, check=False)
        self.retain_cleanup(result.stderr.decode(errors="replace"))
        return result, (self.root / "retired").read_text().strip()

    def retire_probe(self, process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.communicate(timeout=3)

    def test_stdout_and_stderr_stream_binary_bytes_while_native_is_alive(self):
        command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BEHAVIOR": "stream-live"})
        process = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        self.addCleanup(self.retire_probe, process)
        for pipe, expected in ((process.stdout, b"stdout\0\r\n"), (process.stderr, b"stderr\0\r\n")):
            self.assertEqual([pipe], select.select([pipe], [], [], 2)[0])
            self.assertEqual(expected, os.read(pipe.fileno(), len(expected)))
        self.assertIsNone(process.poll())
        self.assertFalse((self.root / "retired").exists())
        observed = json.loads(self.observation.read_text())
        control = json.loads(self.control.read_text())
        scratch = Path(control["directory"])
        self.assertFalse((scratch / "output").exists())
        self.assertFalse((scratch / "command.log").exists())
        self.assertTrue(observed["stdin_eof"])
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=4)
        self.retain_cleanup(stderr.decode())
        self.assertEqual(143, process.returncode, msg=stderr.decode())
        self.assertEqual("true\n", (self.root / "retired").read_text())
        self.assert_client_retired()

    def test_stdout_and_stderr_over_eight_mib_are_not_capped(self):
        result, fact = self.run_stdio(budget="untimed", additions={"NATIVE_BEHAVIOR": "stream-large"})
        expected = bytes(range(256)) * 32769
        self.assertEqual(len(expected), len(result.stdout))
        self.assertEqual(len(expected), len(result.stderr))
        self.assertEqual(0, result.returncode, msg=result.stderr[:200].decode(errors="replace"))
        self.assertEqual(expected, result.stdout)
        self.assertEqual(expected, result.stderr)
        self.assertEqual("true", fact)
        self.assert_client_retired()

    def test_status_and_signal_dispositions_match_timed_and_untimed_calls(self):
        for budget in ("20", "untimed"):
            for status in (0, 7, 137):
                with self.subTest(budget=budget, status=status):
                    result, fact = self.run_stdio(
                        budget=budget, additions={"NATIVE_BEHAVIOR": "stream-finite", "NATIVE_STATUS": str(status)}
                    )
                    self.assertEqual(status, result.returncode, msg=result.stderr.decode())
                    self.assertEqual(b"stdout\0\r\n", result.stdout)
                    self.assertEqual(b"stderr\0\r\n", result.stderr)
                    self.assertNotIn(b"private capped output", result.stderr)
                    self.assertNotIn(b"command.log", result.stderr)
                    self.assertEqual("true", fact)
                    observed = json.loads(self.observation.read_text())
                    for key in ("sigint_ignored", "sigterm_ignored", "sighup_ignored"):
                        self.assertFalse(observed[key], msg=key)
                    self.assertTrue(observed["stdin_eof"])
                    self.assert_client_retired()

    def test_native_closed_outputs_do_not_establish_completion(self):
        command, environment = self.fact_command(
            budget="untimed", additions={"NATIVE_BEHAVIOR": "closed-output", "NATIVE_STATUS": "7"}
        )
        process = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        self.addCleanup(self.retire_probe, process)
        self.await_file(self.root / "outputs-closed")
        observed = json.loads(self.observation.read_text())
        self.assertFalse(Path("/proc", str(observed["pid"]), "fd", "1").exists())
        self.assertFalse(Path("/proc", str(observed["pid"]), "fd", "2").exists())
        self.assertIsNone(process.poll())
        self.assertFalse((self.root / "retired").exists())
        _, stderr = process.communicate(timeout=4)
        self.assertEqual(7, process.returncode, msg=stderr.decode())
        self.assertEqual("true\n", (self.root / "retired").read_text())
        self.assert_client_retired()

    def test_held_public_pipe_descendant_is_retired_before_true_fact(self):
        result, fact = self.run_stdio(budget="untimed", additions={"NATIVE_BEHAVIOR": "held-descendant", "NATIVE_STATUS": "7"})
        self.assertEqual(7, result.returncode, msg=result.stderr.decode())
        self.assertEqual("true", fact)
        self.assert_client_retired()

    def test_blocked_public_output_cancellation_retires_every_owned_process(self):
        for stream in ("stdout", "stderr"):
            for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                with self.subTest(stream=stream, signal=signum):
                    self.observation.unlink(missing_ok=True)
                    (self.root / "write-complete").unlink(missing_ok=True)
                    (self.root / "retired").unlink(missing_ok=True)
                    command, environment = self.fact_command(
                        budget="untimed",
                        additions={
                            "NATIVE_BEHAVIOR": "blocked-output",
                            "NATIVE_BLOCK_STREAM": stream,
                            "NATIVE_CLEANUP_FAILURE": "after-delete",
                        },
                    )
                    process = subprocess.Popen(
                        command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                    )
                    self.addCleanup(self.retire_probe, process)
                    self.await_file(self.observation)
                    observed = json.loads(self.observation.read_text())
                    wait_channel = Path("/proc", str(observed["pid"]), "wchan")
                    deadline = time.monotonic() + 2
                    while (
                        wait_channel.read_text().strip() not in {"pipe_write", "anon_pipe_write"} and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    self.assertIn(wait_channel.read_text().strip(), {"pipe_write", "anon_pipe_write"})
                    self.assertFalse((self.root / "write-complete").exists())
                    try:
                        process.send_signal(signum)
                        # Keep both consumers unread until owner exit. communicate
                        # would drain a full stderr pipe and conceal owner writes.
                        self.assertEqual(128 + signum, process.wait(timeout=4))
                        self.assertEqual("true\n", (self.root / "retired").read_text())
                        self.assert_client_retired()
                        self.assertFalse(Path(json.loads(self.control.read_text())["directory"]).exists())
                    finally:
                        for identifier in (observed["pid"], observed["descendant"]):
                            self.kill_recorded_fixture(identifier)
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                        _, stderr = process.communicate(timeout=4)
                        self.retain_cleanup(stderr.decode())

    def test_first_stdio_child_sees_false_before_pid_capture_and_birth_cancellation(self):
        source = self.helper.read_text()
        launch = '        bash -c "$anchor_program" bash "$scratch/status" "$owning_parent" "$budget" "$timed_program" "$@" &\n'
        self.assertEqual(1, source.count(launch))
        sampled = (
            '        (printf "%s\\n" "$inference_retirement_confirmed" "$BASHPID" > "$NATIVE_BIRTH"; '
            'exec bash -c "$anchor_program" bash "$scratch/status" "$owning_parent" "$budget" "$timed_program" "$@") &\n'
        )
        source = source.replace(launch, sampled, 1)
        capture = "    leader=$!\n"
        self.assertEqual(1, source.count(capture))
        self.helper.write_text(source.replace(capture, "    sleep 0.25\n" + capture, 1))
        receipt = self.root / "birth"
        for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum):
                receipt.unlink(missing_ok=True)
                command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BIRTH": str(receipt)})
                process = subprocess.Popen(
                    command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
                )
                self.addCleanup(self.retire_probe, process)
                self.await_file(receipt)
                fact, anchor = receipt.read_text().splitlines()
                self.assertEqual("false", fact)
                process.send_signal(signum)
                _, stderr = process.communicate(timeout=4)
                self.assertEqual(128 + signum, process.returncode, msg=stderr.decode())
                self.assertEqual("true\n", (self.root / "retired").read_text())
                self.assert_retired(int(anchor))
                self.assertFalse(self.observation.exists())

    def test_receipt_and_startup_faults_do_not_invent_capture_evidence(self):
        for additions, expected, fact in (
            ({"NATIVE_PUBLICATION_FAILURE": "ready"}, 1, "false"),
            ({"NATIVE_PUBLICATION_FAILURE": "release"}, 29, "true"),
            ({"NATIVE_PUBLICATION_FAILURE": "status", "NATIVE_BEHAVIOR": "held-descendant"}, 1, "true"),
            ({"NATIVE_STATUS_MUTATION": "fifo", "NATIVE_BEHAVIOR": "held-descendant"}, 1, "true"),
            ({"NATIVE_STATUS_MUTATION": "read-error", "NATIVE_BEHAVIOR": "held-descendant"}, 1, "true"),
            ({"NATIVE_CLEANUP_FAILURE": "1"}, 23, "true"),
            ({"NATIVE_CLEANUP_FAILURE": "1", "NATIVE_STATUS": "7"}, 7, "true"),
            ({"NATIVE_CLEANUP_FAILURE": "after-delete"}, 23, "true"),
            ({"NATIVE_CLEANUP_FAILURE": "after-delete", "NATIVE_STATUS": "7"}, 7, "true"),
        ):
            with self.subTest(additions=additions):
                result, observed_fact = self.run_stdio(budget="untimed", additions=additions)
                self.assertEqual(expected, result.returncode, msg=result.stderr.decode())
                self.assertEqual(fact, observed_fact)
                self.assertNotIn(b"command.log", result.stderr)
                self.assertNotIn(b"private capped output", result.stderr)
                if additions.get("NATIVE_CLEANUP_FAILURE") == "after-delete":
                    self.assertFalse(Path(json.loads(self.control.read_text())["directory"]).exists())
                    self.assertEqual(b"", result.stderr)
                self.assert_retired(json.loads(self.control.read_text())["anchor"])
                if self.observation.exists():
                    self.assert_client_retired()

    def test_fact_is_false_before_anchor_join(self):
        source = self.helper.read_text()
        join = '            wait "$leader" 2>/dev/null || :\n'
        self.assertEqual(1, source.count(join))
        receipt = self.root / "before-join"
        self.helper.write_text(
            source.replace(join, '            printf "%s\\n" "$inference_retirement_confirmed" > "$NATIVE_JOIN_FACT"\n' + join, 1)
        )
        result, fact = self.run_stdio(budget="untimed", additions={"NATIVE_JOIN_FACT": str(receipt)})
        self.assertEqual(0, result.returncode, msg=result.stderr.decode())
        self.assertEqual("false\n", receipt.read_text())
        self.assertEqual("true", fact)

    def test_unknown_retirement_exit_is_not_public_pipe_eof(self):
        command, environment = self.fact_command(budget="untimed", additions={"NATIVE_BEHAVIOR": "stream-live"})
        process = subprocess.Popen(
            command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True
        )
        self.addCleanup(self.retire_probe, process)
        self.await_file(self.observation)
        observed = json.loads(self.observation.read_text())
        os.kill(observed["parent"], signal.SIGKILL)
        try:
            self.assertEqual(1, process.wait(timeout=4))
            self.assertEqual("false\n", (self.root / "retired").read_text())
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                while True:
                    try:
                        chunk = os.read(pipe.fileno(), 4096)
                    except BlockingIOError:
                        break
                    self.assertNotEqual(b"", chunk, msg="unconfirmed surviving writer unexpectedly reached EOF")
            self.assertTrue(Path("/proc", str(observed["descendant"])).exists())
        finally:
            self.kill_recorded_fixture(observed["pid"])
            self.kill_recorded_fixture(observed["descendant"])
        for pipe in (process.stdout, process.stderr):
            os.set_blocking(pipe.fileno(), True)
        _, stderr = process.communicate(timeout=4)
        self.retain_cleanup(stderr.decode())
        self.assertNotIn(b"command.log", stderr)


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
    if name == "rm" and os.environ.get("NATIVE_CLEANUP_FAILURE") == "after-delete":
        # Actual deletion plus a secondary failure must not invent retained files
        # or block the stdio owner by writing another message to its native pipe.
        subprocess.run(["/bin/rm", *arguments], check=True)
        sys.stderr.write("fixture cleanup failed after deletion\n")
        return 23
    os.execv("/bin/" + name, [name, *arguments])
    return 97


def native_client():
    """Model a finite native status, resistant group member or output-only EOF."""
    observation = Path(os.environ["NATIVE_OBSERVATION"])
    behavior = os.environ.get("NATIVE_BEHAVIOR", "")
    observed = {
        "pid": os.getpid(),
        "parent": os.getppid(),
        "sigint_ignored": signal.getsignal(signal.SIGINT) == signal.SIG_IGN,
        "sigterm_ignored": signal.getsignal(signal.SIGTERM) == signal.SIG_IGN,
        "sighup_ignored": signal.getsignal(signal.SIGHUP) == signal.SIG_IGN,
        "stdin_eof": os.read(0, 1) == b"",
    }
    if behavior in {"resistant", "held-descendant", "stream-live", "blocked-output"}:
        descendant = subprocess.Popen(["/bin/bash", "-c", "trap '' HUP INT TERM; exec sleep 60"])
        observed["descendant"] = descendant.pid
    observation.with_suffix(".pending").write_text(json.dumps(observed))
    observation.with_suffix(".pending").replace(observation)
    if behavior in {"stream-live", "stream-finite"}:
        os.write(1, b"stdout\0\r\n")
        os.write(2, b"stderr\0\r\n")
    if behavior == "stream-live":
        time.sleep(60)
    if behavior == "stream-large":
        data = bytes(range(256)) * 32769
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        sys.stderr.buffer.write(data)
        sys.stderr.buffer.flush()
    if behavior == "blocked-output":
        stream = os.environ["NATIVE_BLOCK_STREAM"]
        if stream == "stdout":
            sys.stdout.buffer.write(b"x" * 8388609)
            sys.stdout.buffer.flush()
        elif stream == "stderr":
            sys.stderr.buffer.write(b"x" * 8388609)
            sys.stderr.buffer.flush()
        else:
            raise ValueError("Blocked-output fixture requires explicit stdout or stderr")
        (observation.parent / "write-complete").write_text("finished")
    if behavior == "resistant":
        signal.signal(signal.SIGTERM, lambda *_: (observation.parent / "term-observed").write_text("TERM\n"))
        time.sleep(60)
    if behavior == "late-success":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(0.3)
    if behavior == "slow-success":
        time.sleep(0.4)
    if behavior == "closed-output":
        os.close(1)
        os.close(2)
        (observation.parent / "outputs-closed").write_text("closed")
        time.sleep(0.25)
    return int(os.environ.get("NATIVE_STATUS", "0"))


if __name__ == "__main__":
    command_name = Path(sys.argv[0]).name
    if command_name in {"mv", "head", "rm"}:
        raise SystemExit(fixture_command(command_name))
    if sys.argv[1:] == ["native-client"]:
        raise SystemExit(native_client())
    unittest.main()
