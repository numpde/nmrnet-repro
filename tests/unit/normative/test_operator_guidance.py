"""Suggested commands preserve selected values without executing them."""

from tools.container_guard import require_container

require_container()

import subprocess
import tempfile
import unittest
from pathlib import Path


class OperatorGuidanceTests(unittest.TestCase):
    def test_suggested_command_round_trips_literal_values_without_running_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            sentinel = Path(temporary) / "executed"
            selection = f"WHEELHOUSE={temporary}/shared wheels $(touch {sentinel})"
            result = subprocess.run(
                [
                    "bash",
                    "-eu",
                    "-c",
                    'source tools/operator_guidance.sh; next_make_command "Next:" packages/image "$1"',
                    "guidance",
                    selection,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertFalse(sentinel.exists())
            command = result.stdout.splitlines()[-1]
            received = subprocess.run(
                ["bash", "-eu", "-c", 'make() { printf "%s\\0" "$@"; }; ' + command],
                capture_output=True,
                check=True,
            )
            self.assertEqual(received.stdout.split(b"\0")[:-1], [b"packages/image", selection.encode()])
            self.assertFalse(sentinel.exists())
