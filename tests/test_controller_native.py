#!/usr/bin/env python3
"""Probe cooperative local ownership; controller cleanup remains its own fact."""

from tools.container_guard import require_container

require_container()

import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests import test_inference_native as native_fixture


class ControllerNativeTests(unittest.TestCase):
    def setUp(self):
        # Compose the finite files/tools without discovering its original tests.
        self.fixture = native_fixture.CapturedNativeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.helper = self.fixture.helper

    def launch(self, *, status=0, hold=False, budget="untimed", result="stdio", prefix="", additions=None):
        for name in (
            "client.json",
            "control.json",
            "signals",
            "finish",
            "outcome",
            "diagnostic",
            "pre-release-phase",
            "pre-release-cancelled",
            "pre-release-continue",
        ):
            (self.root / name).unlink(missing_ok=True)
        environment = self.fixture.environment | {
            "NATIVE_STATUS": str(status),
            "CONTROLLER_HOLD": "1" if hold else "0",
            "CONTROLLER_ROOT": str(self.root),
        }
        if additions is not None:
            environment |= additions
        command = [
            "/bin/bash",
            "-euc",
            'source "$1"; inference_diagnostic=; inference_cancellation_status=0; '
            "trap 'status=$?; trap - EXIT; "
            'printf "%s %s %s\\n" "$status" "$inference_cancellation_status" "$inference_retirement_confirmed" > "$CONTROLLER_ROOT/outcome"; '
            'printf "%s" "$inference_diagnostic" > "$CONTROLLER_ROOT/diagnostic"; exit "$status"\' EXIT; '
            + prefix
            + 'controller_native probe "$2" "$3" "$4" "$5" controller-client',
            "bash",
            str(self.helper),
            budget,
            result,
            sys.executable,
            str(Path(__file__).resolve()),
        ]
        stdout = (self.root / "stdout").open("wb")
        stderr = (self.root / "stderr").open("wb")
        self.addCleanup(stdout.close)
        self.addCleanup(stderr.close)
        return subprocess.Popen(command, env=environment, stdout=stdout, stderr=stderr, start_new_session=True)

    def wait_terminal(self, process, *, seconds=4):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            observed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG)
            if observed is not None:
                return observed
            time.sleep(0.01)
        raise self.failureException("The owned caller did not reach a waitable terminal state.")

    def observe_anchor(self, process):
        control = json.loads(self.fixture.control.read_text())
        anchor = control["anchor"]
        directory = Path(control["directory"])
        self.assertEqual(anchor, os.getpgid(anchor))
        self.assertEqual(process.pid, os.getsid(anchor))
        try:
            command_line = Path("/proc", str(anchor), "cmdline").read_bytes()
        except FileNotFoundError as error:
            # Only disappearance of the already selected proc entry is a race;
            # missing fixture/control/evidence files must still fail normally.
            raise ProcessLookupError(anchor) from error
        arguments = command_line.split(b"\0")[:-1]
        self.assertEqual([b"bash", b"-c"], arguments[:2])
        self.assertTrue(
            arguments[2].startswith(
                b"\n        receipt=$1 parent=$2 budget=$3 timed_program=$4 deadline_signal=$5 grace=$6; shift 6\n"
            )
        )
        self.assertEqual([b"bash", os.fsencode(directory / "status"), str(process.pid).encode(), b"untimed"], arguments[3:7])
        self.assertEqual([b"", b""], arguments[8:10])
        ready = directory / "status.ready"
        self.assertTrue(ready.is_file() and not ready.is_symlink())
        self.assertEqual(f"{anchor}\n".encode(), ready.read_bytes())
        return anchor, directory

    def rescue(self, process, admitted):
        # Never reap the session owner before checking an observed held group.
        if process.returncode is not None:
            return
        if os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                self.wait_terminal(process, seconds=1)
            except self.failureException:
                os.killpg(process.pid, signal.SIGKILL)
                self.wait_terminal(process, seconds=2)
        if admitted is not None:
            anchor, directory = admitted
            try:
                current = self.observe_anchor(process)
            except ProcessLookupError:
                current = None
            if current is not None:
                self.assertEqual(admitted, current)
                os.killpg(anchor, signal.SIGKILL)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                try:
                    os.killpg(anchor, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                raise self.failureException("The admitted held group survived the fixture's bounded rescue.")
            subprocess.run(["/bin/rm", "-rf", "--", str(directory)], check=True)
        process.communicate(timeout=3)

    def test_original_signals_wait_for_the_same_delayed_completion(self):
        process = self.launch(hold=True)
        admitted = None
        try:
            self.fixture.await_file(self.fixture.observation)
            admitted = self.observe_anchor(process)
            observed = json.loads(self.fixture.observation.read_text())
            process.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if (self.root / "signals").exists() and (self.root / "signals").read_text() == "15\n":
                    break
                time.sleep(0.01)
            self.assertEqual("15\n", (self.root / "signals").read_text())
            started = time.monotonic()
            while time.monotonic() - started < 2.2:
                self.assertIsNone(os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG))
                self.assertTrue(Path("/proc", str(observed["pid"])).exists())
                self.assertEqual(admitted, self.observe_anchor(process))
                self.assertFalse((admitted[1] / "status").exists())
                time.sleep(0.02)
            process.send_signal(signal.SIGINT)
            deadline = time.monotonic() + 3
            while (self.root / "signals").read_text() != "15\n2\n" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual("15\n2\n", (self.root / "signals").read_text())
            self.assertFalse((admitted[1] / "status.deadline").exists())
            (self.root / "finish").write_text("finish\n")
            terminal = self.wait_terminal(process)
            self.assertEqual(130, terminal.si_status)
            self.assertEqual("130 130 true\n", (self.root / "outcome").read_text())
            self.assertEqual(b"stdout\0\xff\n", (self.root / "stdout").read_bytes())
            self.assertEqual(b"stderr\0\xfe\n", (self.root / "stderr").read_bytes())
            self.assertEqual("", (self.root / "diagnostic").read_text())
            self.fixture.assert_client_retired()
            with self.assertRaises(ProcessLookupError):
                os.killpg(admitted[0], 0)
            self.assertFalse(admitted[1].exists())
            process.communicate(timeout=3)
            self.assertEqual(130, process.returncode)
        finally:
            self.rescue(process, admitted)

    def test_ordinary_status_and_closed_entry_contract_preserve_provenance(self):
        for status in (0, 143):
            with self.subTest(status=status):
                process = self.launch(status=status, hold=True)
                admitted = None
                try:
                    self.fixture.await_file(self.fixture.observation)
                    admitted = self.observe_anchor(process)
                    (self.root / "finish").write_text("finish\n")
                    terminal = self.wait_terminal(process)
                    self.assertEqual(status, terminal.si_status)
                    self.assertEqual(f"{status} 0 true\n", (self.root / "outcome").read_text())
                    self.assertEqual(b"stdout\0\xff\n", (self.root / "stdout").read_bytes())
                    self.assertEqual(b"stderr\0\xfe\n", (self.root / "stderr").read_bytes())
                    self.assertEqual("", (self.root / "diagnostic").read_text())
                    self.fixture.assert_client_retired()
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(admitted[0], 0)
                    self.assertFalse(admitted[1].exists())
                    process.communicate(timeout=3)
                    self.assertEqual(status, process.returncode)
                finally:
                    self.rescue(process, admitted)
        for budget, result in (("10", "stdio"), ("untimed", "discard")):
            with self.subTest(budget=budget, result=result):
                process = self.launch(budget=budget, result=result)
                try:
                    terminal = self.wait_terminal(process)
                    self.assertEqual(2, terminal.si_status)
                    self.assertEqual("2 0 true\n", (self.root / "outcome").read_text())
                    self.assertFalse(self.fixture.observation.exists())
                    self.assertFalse(self.fixture.control.exists())
                    self.assertEqual(b"", (self.root / "stdout").read_bytes())
                    self.assertEqual(b"", (self.root / "stderr").read_bytes())
                    self.assertEqual(
                        "Cannot own controller: cooperative completion requires untimed stdio.\n",
                        (self.root / "diagnostic").read_text(),
                    )
                    process.communicate(timeout=3)
                    self.assertEqual(2, process.returncode)
                finally:
                    self.rescue(process, None)

    def test_pre_release_cancellation_does_not_dispatch(self):
        seam = '    if "$ready" && [[ $status == 0 && $cancelled == 0 ]]; then\n'
        source = self.helper.read_text()
        self.assertEqual(1, source.count(seam))
        phase = """    printf 'ready\\n' > "$CONTROLLER_ROOT/pre-release-phase"
    while [[ ! -e "$CONTROLLER_ROOT/pre-release-continue" ]]; do
        if [[ $cancelled != 0 && ! -e "$CONTROLLER_ROOT/pre-release-cancelled" ]]; then
            printf '%s\\n' "$cancelled" > "$CONTROLLER_ROOT/pre-release-cancelled"
        fi
        sleep 0.01
    done
"""
        self.helper.write_text(source.replace(seam, phase + seam, 1))
        process = self.launch(hold=True)
        admitted = None
        try:
            self.fixture.await_file(self.root / "pre-release-phase")
            admitted = self.observe_anchor(process)
            self.assertFalse((admitted[1] / "status.release").exists())
            self.assertFalse(self.fixture.observation.exists())
            process.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                acknowledgement = self.root / "pre-release-cancelled"
                if acknowledgement.exists() and acknowledgement.read_text() == "143\n":
                    break
                time.sleep(0.01)
            self.assertEqual("143\n", (self.root / "pre-release-cancelled").read_text())
            (self.root / "pre-release-continue").write_text("continue\n")
            terminal = self.wait_terminal(process)
            self.assertEqual(143, terminal.si_status)
            self.assertEqual("143 143 true\n", (self.root / "outcome").read_text())
            self.assertFalse(self.fixture.observation.exists())
            control = json.loads(self.fixture.control.read_text())
            self.assertFalse((Path(control["directory"]) / "status.release").exists())
            self.fixture.assert_retired(control["anchor"])
            with self.assertRaises(ProcessLookupError):
                os.killpg(control["anchor"], 0)
            self.assertFalse(Path(control["directory"]).exists())
            self.assertEqual(b"", (self.root / "stdout").read_bytes())
            self.assertEqual(b"", (self.root / "stderr").read_bytes())
            self.assertEqual("", (self.root / "diagnostic").read_text())
            process.communicate(timeout=3)
            self.assertEqual(143, process.returncode)
        finally:
            self.rescue(process, admitted)

    def test_release_attempt_failure_retains_a_dispatched_controller(self):
        prefix = (
            'mv() { if [[ "${@: -1}" == */status.release ]]; then '
            'command mv "$@"; return 29; else command mv "$@"; fi; }; export -f mv; '
        )
        process = self.launch(hold=True, prefix=prefix)
        admitted = None
        try:
            self.fixture.await_file(self.fixture.observation)
            admitted = self.observe_anchor(process)
            observed = json.loads(self.fixture.observation.read_text())
            self.wait_terminal(process)
            self.assertTrue(Path("/proc", str(observed["pid"])).exists())
            self.assertEqual(29, os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG).si_status)
            self.assertEqual("29 0 false\n", (self.root / "outcome").read_text())
            self.assertTrue((admitted[1] / "status.release").is_file())
            self.assertFalse((admitted[1] / "status").exists())
            self.assertIn("native parent release could not be published", (self.root / "diagnostic").read_text())
            self.assertIn(str(admitted[1]), (self.root / "diagnostic").read_text())
        finally:
            self.rescue(process, admitted)

    def test_untrusted_completion_never_authorizes_controller_kill(self):
        for phase in ("nul", "publication", "deadline", "deadline-fifo"):
            with self.subTest(phase=phase):
                additions = {}
                prefix = ""
                if phase == "publication":
                    additions["NATIVE_PUBLICATION_FAILURE"] = "status"
                elif phase == "deadline":
                    prefix = (
                        'mv() { if [[ "${@: -1}" == */status ]]; then '
                        'printf "expired\\n" > "${@: -1}.deadline"; return 0; '
                        'else command mv "$@"; fi; }; export -f mv; '
                    )
                else:
                    additions["NATIVE_STATUS_MUTATION"] = phase
                process = self.launch(hold=True, prefix=prefix, additions=additions)
                admitted = None
                try:
                    self.fixture.await_file(self.fixture.observation)
                    admitted = self.observe_anchor(process)
                    (self.root / "finish").write_text("finish\n")
                    terminal = self.wait_terminal(process)
                    self.assertEqual(admitted, self.observe_anchor(process))
                    expected = 2 if phase == "nul" else 1
                    self.assertEqual(expected, terminal.si_status)
                    self.assertEqual(f"{expected} 0 false\n", (self.root / "outcome").read_text())
                    self.assertIn(str(admitted[1]), (self.root / "diagnostic").read_text())
                    self.assertTrue(Path("/proc", str(admitted[0])).exists())
                    if phase.startswith("deadline"):
                        deadline = admitted[1] / "status.deadline"
                        if phase == "deadline":
                            self.assertEqual(b"expired\n", deadline.read_bytes())
                        else:
                            self.assertTrue(deadline.is_fifo())
                        self.assertIn("unexpected deadline receipt", (self.root / "diagnostic").read_text())
                    elif phase == "nul":
                        self.assertEqual(b"0\0\n", (admitted[1] / "status").read_bytes())
                        self.assertIn(
                            "completion receipt must contain one byte-exact status", (self.root / "diagnostic").read_text()
                        )
                    else:
                        self.assertFalse((admitted[1] / "status").exists())
                        self.assertIn("completion publication failed", (self.root / "diagnostic").read_text())
                    self.assertNotIn(b"ignored null byte", (self.root / "stderr").read_bytes())
                finally:
                    self.rescue(process, admitted)


def controller_client():
    root = Path(os.environ["CONTROLLER_ROOT"])
    hold = os.environ["CONTROLLER_HOLD"]
    if hold not in {"0", "1"}:
        raise ValueError("Controller fixture requires an explicit hold selector.")
    observed = {"pid": os.getpid(), "parent": os.getppid()}

    def record_signal(signum, _frame):
        with (root / "signals").open("a") as target:
            target.write(f"{signum}\n")

    for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, record_signal)
    if hold == "1":
        descendant = subprocess.Popen(["/bin/bash", "-c", "trap '' HUP INT TERM; exec sleep 60"])
        observed["descendant"] = descendant.pid
        ignored = sum(1 << (signum - 1) for signum in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            fields = dict(
                line.split(":", 1)
                for line in Path("/proc", str(descendant.pid), "status").read_text().splitlines()
                if ":" in line
            )
            if int(fields["SigIgn"].strip(), 16) & ignored == ignored:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("The actual descendant did not install all three ignored signal dispositions.")
    pending = Path(os.environ["NATIVE_OBSERVATION"]).with_suffix(".pending")
    pending.write_text(json.dumps(observed))
    pending.replace(Path(os.environ["NATIVE_OBSERVATION"]))
    os.write(1, b"stdout\0\xff\n")
    os.write(2, b"stderr\0\xfe\n")
    if hold == "1":
        while not (root / "finish").is_file():
            time.sleep(0.01)
    return int(os.environ["NATIVE_STATUS"])


if __name__ == "__main__":
    if sys.argv[1:] == ["controller-client"]:
        raise SystemExit(controller_client())
    unittest.main()
