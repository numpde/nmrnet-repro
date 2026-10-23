"""Literal paths and controlling-terminal answers select the same cache root."""

from tools.container_guard import require_container

require_container()

import fcntl
import os
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path


def own_terminal():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


class WheelhousePathTests(unittest.TestCase):
    def test_relative_and_dotted_paths_resolve_from_the_callers_working_directory(self):
        helper = Path("tools/wheelhouse_path.sh").resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            working = root / "working"
            working.mkdir()
            (root / "shared target").mkdir()
            (working / "cache-link").symlink_to(root / "shared target", target_is_directory=True)
            (working / "cache,link").symlink_to(root / "shared target", target_is_directory=True)
            for selected, expected in (
                ("./cache $token $(touch sentinel)", working / "cache $token $(touch sentinel)"),
                ("../shared cache", root / "shared cache"),
                ("./cache/", working / "cache"),
                ("cache-link/child/", root / "shared target/child"),
                ("cache,link", root / "shared target"),
                ("./unused,component/../cache", working / "cache"),
                (str(working) + "/../shared cache", root / "shared cache"),
                (".", working),
            ):
                with self.subTest(selected=selected):
                    result = subprocess.run(
                        [
                            "bash",
                            "-euc",
                            'source "$1"; select_wheelhouse; printf "%s" "$wheelhouse_directory"',
                            "bash",
                            str(helper),
                        ],
                        cwd=working,
                        env=os.environ | {"NMRNET_WHEELHOUSE_INPUT": selected, "NMRNET_WHEELHOUSE_INPUT_ORIGIN": "command line"},
                        capture_output=True,
                        text=True,
                        start_new_session=True,
                        timeout=3,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, str(expected))
                    self.assertFalse((working / "sentinel").exists())

    def test_supplied_path_preserves_spaces_and_shell_text_as_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            selected = str(Path(temporary) / "shared $token $(touch sentinel)")
            result = subprocess.run(
                ["bash", "-euc", 'source tools/wheelhouse_path.sh; select_wheelhouse; printf "%s" "$wheelhouse_directory"'],
                env=os.environ | {"NMRNET_WHEELHOUSE_INPUT": selected, "NMRNET_WHEELHOUSE_INPUT_ORIGIN": "command line"},
                capture_output=True,
                text=True,
                start_new_session=True,
                timeout=3,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, selected)
            self.assertFalse(Path("sentinel").exists())
            self.assertFalse(Path(selected).exists(), "Selection alone must not create the cache.")

    def test_omitted_value_accepts_a_relative_path_from_the_controlling_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            selected = str(Path(temporary) / "shared cache")
            master, slave = pty.openpty()
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "tests.unit.normative.test_wheelhouse_path",
                    "bash",
                    "-euc",
                    'source tools/wheelhouse_path.sh; select_wheelhouse; printf "SELECTED=%s\\n" "$wheelhouse_directory"',
                ],
                env=os.environ
                | {"NMRNET_WHEELHOUSE_INPUT": "/ignored/environment/cache", "NMRNET_WHEELHOUSE_INPUT_ORIGIN": "environment"},
                stdin=slave,
                stdout=slave,
                stderr=slave,
            )
            os.close(slave)
            received = b""
            try:
                deadline = time.monotonic() + 3
                while b"WHEELHOUSE" not in received:
                    self.assertLess(time.monotonic(), deadline, "The cache prompt did not appear.")
                    if select.select([master], [], [], 0.1)[0]:
                        received += os.read(master, 4096)
                self.assertIsNone(process.poll(), "The caller must await an explicit path, without a default.")
                os.write(master, os.path.relpath(selected).encode() + b"\n")
                expected = ("SELECTED=" + selected).encode()
                while expected not in received:
                    self.assertLess(time.monotonic(), deadline, "The selected path was not returned.")
                    if select.select([master], [], [], 0.1)[0]:
                        received += os.read(master, 4096)
                self.assertEqual(process.wait(timeout=2), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)
                os.close(master)


if __name__ == "__main__":
    # Establish the controlling terminal in a fresh interpreter, rather than
    # running Python callbacks between fork and exec in the unittest process.
    own_terminal()
    os.execvp(sys.argv[1], sys.argv[1:])
