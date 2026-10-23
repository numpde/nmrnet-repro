"""Missing or unsafe cache selections never become an implicit checkout path."""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class WheelhousePathRefusalTests(unittest.TestCase):
    def select(self, value, origin="command line"):
        return subprocess.run(
            ["bash", "-euc", 'source tools/wheelhouse_path.sh; select_wheelhouse; printf "%s" "$wheelhouse_directory"'],
            env=os.environ | {"NMRNET_WHEELHOUSE_INPUT": value, "NMRNET_WHEELHOUSE_INPUT_ORIGIN": origin},
            input="/a/piped/value/is/not/operator-input\n",
            capture_output=True,
            text=True,
            start_new_session=True,
            timeout=3,
            check=False,
        )

    def test_omitted_or_ambient_selection_does_not_read_piped_input(self):
        for origin in ("undefined", "environment", "file"):
            with self.subTest(origin=origin):
                result = self.select("/ambient/cache", origin)
                self.assertEqual(result.returncode, 2)
                self.assertIn("WHEELHOUSE", result.stderr)
                self.assertIn("terminal", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_explicit_empty_and_unsafe_paths_are_refused(self):
        for path in ("", "/", "/tmp/..", "/tmp/cache,other", "/tmp/cache\nother", "/tmp/cache\rother"):
            with self.subTest(path=path):
                result = self.select(path)
                self.assertEqual(result.returncode, 2)
                self.assertIn("WHEELHOUSE", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_nondirectory_paths_and_ancestors_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "file").write_text("owned bytes")
            for path in (root / "file", root / "file/child"):
                with self.subTest(path=path):
                    self.assertEqual(self.select(str(path)).returncode, 2)

    def test_relative_paths_cannot_inherit_mount_delimiters_from_the_working_directory(self):
        helper = Path("tools/wheelhouse_path.sh").resolve()
        with tempfile.TemporaryDirectory() as temporary:
            for name in ("comma,directory", "line\nbreak", "carriage\rreturn"):
                with self.subTest(name=name):
                    working = Path(temporary) / name
                    working.mkdir()
                    result = subprocess.run(
                        ["bash", "-euc", 'source "$1"; select_wheelhouse', "bash", str(helper)],
                        cwd=working,
                        env=os.environ | {"NMRNET_WHEELHOUSE_INPUT": "./cache", "NMRNET_WHEELHOUSE_INPUT_ORIGIN": "command line"},
                        capture_output=True,
                        text=True,
                        timeout=3,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("resolved WHEELHOUSE", result.stderr)

    def test_unrelated_contents_do_not_invalidate_a_shared_cache_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "keep").write_text("unrelated operator data")
            result = self.select(temporary)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, temporary)

    def test_resolved_link_targets_cannot_introduce_mount_delimiters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, name in enumerate(("comma,directory", "ending\n", "ending\r")):
                with self.subTest(name=name):
                    target = root / name
                    target.mkdir()
                    link = root / f"link-{index}"
                    link.symlink_to(target, target_is_directory=True)
                    result = self.select(str(link))
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn("resolved WHEELHOUSE", result.stderr)
