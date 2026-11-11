"""Exercise the complete interactive download against finite engine doubles.

The real local interface inventory is read inside the offline test container.
No proxy, connection, package installer or artifact transfer is executed.
"""

from tools.container_guard import require_container

require_container()

import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import suppress
from pathlib import Path

from repository_checks.fixtures.containers.fixture import LauncherFixture
from repository_checks.quality.sources import ROOT


class AcquisitionInputTests(unittest.TestCase):
    def test_wheelhouse_adapter_orders_workers_and_preserves_failure_kind(self):
        program = r"""
set -euo pipefail
source "$1"
image=fixture:pinned
interface=lo
wheelhouse_warm=false
offline_grants=(--read-only)
offline_app=(python3 -B -m tools.wheelhouse_acquire --probe)
online_grants=(--read-only)
online_app=(python3 -B -m tools.wheelhouse_acquire)
offline_count=0
acquisition_set_route() {
    printf 'route %s\n' "$1" >> "$EVENTS"
}
acquisition_worker() {
    local phase=$1 destination=$2 worker_image=$3 grants_name=$4 app_name=$5
    [[ $worker_image == "$image" ]] || return 97
    [[ $grants_name == "${phase}_grants" && $app_name == "${phase}_app" ]] || return 97
    printf 'worker %s\n' "$phase" >> "$EVENTS"
    if [[ $OWNER_STATUS != 0 ]]; then return "$OWNER_STATUS"; fi
    local result
    if [[ $phase == offline ]]; then
        offline_count=$((offline_count + 1))
        if (( offline_count == 1 )); then result=$INITIAL_STATUS; else result=$FINAL_STATUS; fi
    else
        result=$ONLINE_STATUS
    fi
    printf -v "$destination" '%s' "$result"
}
status=0
nmrnet_acquire_wheelhouse || status=$?
printf 'warm=%s\n' "$wheelhouse_warm"
exit "$status"
"""
        cold = ["worker offline", "route lo", "worker online", "worker offline"]
        cases = (
            ("warm", 0, 0, 0, 0, 0, True, ["worker offline"]),
            ("cold", 3, 0, 0, 0, 0, False, cold),
            ("initial-worker-failure", 2, 0, 0, 0, 2, False, ["worker offline"]),
            ("owner-status-is-not-cache-miss", 3, 0, 0, 3, 3, False, ["worker offline"]),
            ("online-worker-failure", 3, 7, 0, 0, 7, False, cold[:-1]),
            ("final-still-missing", 3, 0, 3, 0, 1, False, cold),
            ("final-worker-failure", 3, 0, 17, 0, 17, False, cold),
        )
        for name, initial, online, final, owner, status, warm, events in cases:
            with self.subTest(case=name):
                log = self.root / ("adapter-" + name)
                result = subprocess.run(
                    ["bash", "-c", program, "bash", str(self.root / "tools/acquisition_wheelhouse.sh")],
                    cwd=self.root,
                    env=self.fixture.environment
                    | {
                        "EVENTS": str(log),
                        "INITIAL_STATUS": str(initial),
                        "ONLINE_STATUS": str(online),
                        "FINAL_STATUS": str(final),
                        "OWNER_STATUS": str(owner),
                    },
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(status, result.returncode, result.stderr)
                self.assertEqual(f"warm={str(warm).lower()}\n", result.stdout)
                self.assertEqual(events, log.read_text().splitlines())
                if name == "final-still-missing":
                    self.assertIn("required inputs remain missing", result.stderr)
                else:
                    self.assertEqual("", result.stderr)
                self.assertFalse(self.fixture.calls())

    def read_prompt(self, process, master, received, marker):
        deadline = time.monotonic() + 8
        while marker not in received:
            self.assertLess(time.monotonic(), deadline, msg=bytes(received))
            self.assertIsNone(process.poll(), msg=bytes(received))
            if select.select([master], [], [], 0.1)[0]:
                received.extend(os.read(master, 4096))

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = LauncherFixture(self.root)
        self.addCleanup(self.fixture.dispose)
        shutil.copyfile(ROOT / "tools/checker.sh", self.root / "tools/checker.sh")
        shutil.copyfile(ROOT / "requirements/checker-dependencies.json", self.root / "requirements/checker-dependencies.json")

    def test_each_acquisition_owner_requires_interface_choice_before_engine_effects(self):
        for owner, arguments in (
            ("wheelhouse.sh", ("download",)),
            ("packages.sh", ("stage",)),
            ("checker.sh", ("download",)),
            ("container.sh", ("download",)),
        ):
            with self.subTest(owner=owner):
                result = self.fixture.run(
                    owner,
                    *arguments,
                    additions={
                        "NMRNET_ARCHIVE_INPUT": "weight.zip",
                        "NMRNET_NETWORK_INTERFACE_INPUT": "",
                        "NMRNET_NETWORK_INTERFACE_INPUT_ORIGIN": "undefined",
                        "NETWORK_INTERFACE": "lo",
                    },
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("supply NETWORK_INTERFACE=", result.stderr)
                self.assertFalse(self.fixture.calls())

    def test_explicit_empty_interface_is_refused_without_prompting(self):
        result = self.fixture.make("packages/wheelhouse", "NETWORK_INTERFACE=")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("not an available network interface", result.stderr)
        self.assertNotIn("interactive terminal", result.stderr)
        self.assertFalse(self.fixture.calls())

    def test_blank_or_unlisted_terminal_choices_do_not_select_a_default_or_execute_text(self):
        for answer in (b"", b"999999999999999999999999999999999999999999", b"$(touch sentinel)"):
            with self.subTest(answer=answer):
                master, slave = pty.openpty()
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-B",
                        "-m",
                        "tests.unit.normative.test_wheelhouse_path",
                        "bash",
                        "-euc",
                        'source "$1"; select_acquisition_interface',
                        "bash",
                        str(self.root / "tools/acquisition_interface.sh"),
                    ],
                    cwd=self.root,
                    env=self.fixture.environment
                    | {"NMRNET_NETWORK_INTERFACE_INPUT": "", "NMRNET_NETWORK_INTERFACE_INPUT_ORIGIN": "undefined"},
                    stdin=slave,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                os.close(slave)
                try:
                    self.read_prompt(process, master, bytearray(), b"]: ")
                    os.write(master, answer + b"\n")
                    stdout, stderr = process.communicate(timeout=3)
                    self.assertEqual(process.returncode, 2, stdout + stderr)
                    self.assertIn("selection must be one listed number", stderr)
                    self.assertFalse((self.root / "sentinel").exists())
                    self.assertFalse(self.fixture.calls())
                finally:
                    if process.poll() is None:
                        with suppress(ProcessLookupError):
                            os.killpg(process.pid, signal.SIGKILL)
                        process.communicate(timeout=3)
                    os.close(master)

    def test_plain_make_asks_for_both_inputs_once_and_reuses_the_interface(self):
        master, slave = pty.openpty()
        environment = self.fixture.environment.copy()
        # Match LauncherFixture.make: inherited Make controls must not supply
        # selectors to this proof of an invocation with both inputs omitted.
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"):
            if name in environment:
                del environment[name]
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-m",
                "tests.unit.normative.test_wheelhouse_path",
                "make",
                "--no-print-directory",
                "-C",
                str(self.root),
                "DOCKER_CONTEXT=fixture-local",
                "packages/wheelhouse",
                "SHARED_RUNTIME_IMAGE=sha256:" + "a" * 64,
            ],
            env=environment,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(slave)
        received = bytearray()

        try:
            self.read_prompt(process, master, received, b"WHEELHOUSE (absolute or relative path): ")
            self.assertFalse(self.fixture.calls())
            os.write(master, b"shared wheels\n")
            self.read_prompt(process, master, received, b"Choice [1-")
            self.read_prompt(process, master, received, b"]: ")
            self.assertFalse(self.fixture.calls())
            self.assertFalse((self.root / "shared wheels").exists())
            listed = re.findall(rb"  ([1-9][0-9]*)\) ([A-Za-z0-9_.:-]+) \[[^\r\n]+\]\r?\n", received)
            self.assertEqual({name.decode() for _, name in listed}, {path.name for path in Path("/sys/class/net").iterdir()})
            choice = next(number for number, name in listed if name == b"lo")
            os.write(master, choice + b"\n")
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stdout + stderr + bytes(received).decode())
            self.assertEqual(bytes(received).count(b"Select NETWORK_INTERFACE"), 1)
            self.assertNotIn("Select NETWORK_INTERFACE", stdout + stderr)
            creates = [call["arguments"] for call in self.fixture.calls() if call["arguments"][:1] == ["create"]]
            proxy, worker = creates
            self.assertEqual(proxy[proxy.index("--interface") + 1], "lo")
            self.assertIn(f"type=bind,src={self.root}/shared wheels,dst=/wheelhouse", worker)
            self.assertEqual(worker[worker.index("--pool") + 1], "/wheelhouse")
        finally:
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    for call in self.fixture.calls():
                        with suppress(ProcessLookupError):
                            os.killpg(call["process_group"], signal.SIGKILL)
                    process.kill()
                    process.communicate(timeout=3)
            os.close(master)
