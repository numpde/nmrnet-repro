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
                        "NMRNET_WLAN_INTERFACE_INPUT": "",
                        "NMRNET_WLAN_INTERFACE_INPUT_ORIGIN": "undefined",
                        "WLAN_INTERFACE": "lo",
                    },
                )
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("supply WLAN_INTERFACE=", result.stderr)
                self.assertFalse(self.fixture.calls())

    def test_explicit_empty_interface_is_refused_without_prompting(self):
        result = self.fixture.make("packages/wheelhouse", "WLAN_INTERFACE=")
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
                    | {"NMRNET_WLAN_INTERFACE_INPUT": "", "NMRNET_WLAN_INTERFACE_INPUT_ORIGIN": "undefined"},
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
                "packages/wheelhouse",
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
            listed = re.findall(rb"  ([1-9][0-9]*)\) ([A-Za-z0-9_.:-]+)\r?\n", received)
            self.assertEqual({name.decode() for _, name in listed}, {path.name for path in Path("/sys/class/net").iterdir()})
            choice = next(number for number, name in listed if name == b"lo")
            os.write(master, choice + b"\n")
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stdout + stderr + bytes(received).decode())
            self.assertEqual(bytes(received).count(b"Select WLAN_INTERFACE"), 1)
            self.assertNotIn("Select WLAN_INTERFACE", stdout + stderr)
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
