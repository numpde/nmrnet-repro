"""Host retirement decisions against a recording engine, inside the test container."""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class WorkerRetirementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cid = "a" * 64
        (self.root / "worker.cid").write_text(self.cid + "\n")
        self.engine = self.root / "engine"
        self.engine.write_text(
            "#!/bin/bash\nset -eu\n"
            'printf "%s\\n" "$*" >> "$RECEIPTS/calls"\n'
            'if [[ "$CANCEL_OPERATION" == "$1" ]]; then kill -TERM "$OWNER_PID"; sleep 0.1; fi\n'
            'if [[ "$CAP_OPERATION" == "$1" ]]; then head -c 4096 /dev/zero >&2; exit 0; fi\n'
            'case "$1" in\n'
            'stop) printf "private stop evidence\\n" >&2; exit "$STOP_STATUS" ;;\n'
            'inspect) cat "$RECEIPTS/inspection"; exit "$INSPECT_STATUS" ;;\n'
            'logs) printf "bounded fixture log\\n"; exit "$LOG_STATUS" ;;\n'
            'rm) exit "$REMOVE_STATUS" ;;\n'
            "*) exit 99 ;;\nesac\n"
        )
        self.engine.chmod(0o700)
        (self.root / "inspection").write_text(self.cid + " exited false 0 false\n")

    # silent-defaults: allow-contract -- Normal retirement succeeds; each fault test explicitly selects the failing operation or prior ownership state.
    def run_retirement(self, *, stop_status=0, inspect_status=0, remove_status=0, log_status=0, retired="true", cancel="", cap=""):
        return subprocess.run(
            [
                "bash",
                "-euc",
                "source tools/inference_native.sh; source tools/worker_retirement.sh; "
                "export OWNER_PID=$BASHPID; "
                "inference_retirement_confirmed=$1; "
                'if worker_container_retire "$2" "$3" bash "$4"; then status=0; else status=$?; fi; '
                'printf "%s\\n" "$worker_retirement_diagnostic"; exit "$status"',
                "bash",
                retired,
                self.cid,
                str(self.root),
                str(self.engine),
            ],
            env=os.environ
            | {
                "RECEIPTS": str(self.root),
                "STOP_STATUS": str(stop_status),
                "INSPECT_STATUS": str(inspect_status),
                "REMOVE_STATUS": str(remove_status),
                "LOG_STATUS": str(log_status),
                "CANCEL_OPERATION": cancel,
                "CAP_OPERATION": cap,
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def calls(self):
        path = self.root / "calls"
        return path.read_text().splitlines() if path.exists() else []

    def test_exact_stopped_container_is_removed_and_private_cause_retained(self):
        result = self.run_retirement()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(["stop", "inspect", "logs", "rm"], [line.split()[0] for line in self.calls()])
        self.assertTrue(all(line.endswith(self.cid) for line in self.calls()))
        self.assertNotIn("private stop evidence", result.stdout + result.stderr)
        self.assertEqual("private stop evidence\n", (self.root / "retirement/stop.stderr").read_text())

    def test_missing_ownership_receipt_or_unretired_client_prevents_engine_calls(self):
        (self.root / "worker.cid").unlink()
        result = self.run_retirement()
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.calls())
        (self.root / "worker.cid").write_text(self.cid)
        result = self.run_retirement(retired="false")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([], self.calls())

    def test_created_but_never_started_container_can_be_removed(self):
        (self.root / "inspection").write_text(self.cid + " created false 0 false\n")
        result = self.run_retirement()
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(["stop", "inspect", "logs", "rm"], [line.split()[0] for line in self.calls()])

    def test_stop_failure_is_not_retirement_or_permission_to_repeat(self):
        result = self.run_retirement(stop_status=7)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(1, len(self.calls()))
        self.assertIn("stop", result.stdout)
        again = self.run_retirement()
        self.assertNotEqual(0, again.returncode)
        self.assertEqual(1, len(self.calls()))

    def test_only_exact_stopped_observation_authorizes_removal(self):
        for observation in (
            "b" * 64 + " exited false 0 false\n",
            self.cid + " running true 17 false\n",
            self.cid + " exited false 17 false\n",
            self.cid + " exited false 0 true\n",
            self.cid + " exited false 0 false\n\n",
            self.cid + " exited false 0 false\x00\n",
            self.cid + " exited false 0 false\n" + "x" * 8192,
        ):
            with self.subTest(observation=observation[:90]):
                # Each scenario owns a fresh allocation and receipt namespace.
                probe = WorkerRetirementTests()
                probe.setUp()
                try:
                    (probe.root / "inspection").write_text(observation)
                    result = probe.run_retirement()
                    self.assertNotEqual(0, result.returncode)
                    self.assertEqual(["stop", "inspect"], [line.split()[0] for line in probe.calls()])
                finally:
                    probe.doCleanups()

    def test_inspection_and_removal_failures_remain_unconfirmed(self):
        result = self.run_retirement(inspect_status=8)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(2, len(self.calls()))
        self.assertIn("inspect", result.stdout)

    def test_stopped_but_unremoved_is_not_success(self):
        result = self.run_retirement(remove_status=9)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(4, len(self.calls()))
        self.assertIn("remove", result.stdout)

    def test_failed_logs_preserve_native_status_without_inventing_caller_cancellation(self):
        for status in (37, 143):
            with self.subTest(status=status):
                probe = WorkerRetirementTests()
                probe.setUp()
                try:
                    result = probe.run_retirement(log_status=status)
                    self.assertEqual(result.returncode, 2)
                    self.assertIn(f"status {status}, cancellation 0, local client retired true", result.stdout)
                    self.assertEqual([line.split()[0] for line in probe.calls()], ["stop", "inspect", "logs"])
                finally:
                    probe.doCleanups()

    def test_cancellation_during_each_operation_stops_sequence_and_preserves_receipts(self):
        for operation, count in (("stop", 1), ("inspect", 2), ("logs", 3), ("rm", 4)):
            with self.subTest(operation=operation):
                probe = WorkerRetirementTests()
                probe.setUp()
                try:
                    result = probe.run_retirement(cancel=operation)
                    self.assertNotEqual(0, result.returncode)
                    self.assertIn("cancellation 143", result.stdout)
                    self.assertEqual(count, len(probe.calls()))
                    self.assertTrue((probe.root / "retirement/stop.stderr").exists())
                    self.assertNotEqual(0, probe.run_retirement().returncode)
                    self.assertEqual(count, len(probe.calls()))
                finally:
                    probe.doCleanups()

    def test_success_status_with_at_cap_diagnostic_is_not_complete_evidence(self):
        result = self.run_retirement(cap="stop")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(1, len(self.calls()))
        self.assertIn("capture limit", result.stdout)
        self.assertEqual(4096, (self.root / "retirement/stop.stderr").stat().st_size)
