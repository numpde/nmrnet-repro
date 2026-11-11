"""A common pool supplies lock-selected wheels without owning other projects' files."""

from tools.container_guard import require_container

require_container()

import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.unit.wheel_pool_fixtures import locked, wheel

from tools import wheelhouse


class WheelPoolTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / "common pool"
        self.pool.mkdir()
        self.alpha = wheel(self.pool, "pool-alpha", "1.0")
        self.foreign = wheel(self.pool, "pool-alpha", "1.0", "cp27-cp27m-win32")
        self.beta = wheel(self.pool, "pool-beta", "2.0")
        wheel(self.pool, "pool-alpha", "2.0")
        (self.pool / "other-project.txt").write_bytes(b"leave this alone")
        self.lock = locked(self.root / "alpha.lock", [("pool-alpha", "1.0", [self.alpha, self.foreign])])

    def test_pip_selects_compatible_locked_subset_from_one_flat_pool(self):
        before = {path.name: path.read_bytes() for path in self.pool.iterdir()}
        identity = wheelhouse.select_wheels(self.lock, self.pool)
        self.assertEqual(identity["files"], {self.alpha.name: hashlib.sha256(self.alpha.read_bytes()).hexdigest()})
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.pool.iterdir()})
        second = locked(self.root / "beta.lock", [("pool-beta", "2.0", [self.beta])])
        self.assertEqual(set(wheelhouse.select_wheels(second, self.pool)["files"]), {self.beta.name})

    def test_report_exposes_only_a_successful_offline_selection(self):
        report = io.StringIO()
        identity = wheelhouse.select_wheels(self.lock, self.pool, report_output=report)
        selected = json.loads(report.getvalue())
        self.assertEqual(selected["version"], "1")
        self.assertEqual(len(selected["install"]), 1)
        self.assertEqual(
            selected["install"][0]["download_info"]["archive_info"]["hashes"]["sha256"], identity["files"][self.alpha.name]
        )
        self.alpha.write_bytes(b"invalid wheel")
        report = io.StringIO()
        with self.assertRaises(subprocess.CalledProcessError):
            wheelhouse.select_wheels(self.lock, self.pool, report_output=report)
        self.assertEqual(report.getvalue(), "")

    def test_report_command_emits_pip_json_for_the_captured_lock(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "tools.wheelhouse",
                "report",
                "--lock",
                str(self.lock),
                "--lock-sha256",
                hashlib.sha256(self.lock.read_bytes()).hexdigest(),
                "--pool",
                str(self.pool),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        selected = json.loads(result.stdout)
        self.assertEqual(selected["version"], "1")
        self.assertEqual(
            selected["install"][0]["download_info"]["archive_info"]["hashes"]["sha256"],
            hashlib.sha256(self.alpha.read_bytes()).hexdigest(),
        )

    def test_only_incompatible_cached_wheel_fails_offline(self):
        self.alpha.unlink()
        with self.assertRaises(subprocess.CalledProcessError):
            wheelhouse.select_wheels(self.lock, self.pool)

    def test_bad_selected_hash_refuses_without_modifying_the_pool(self):
        self.alpha.write_bytes(b"corrupt cached wheel")
        with self.assertRaises(subprocess.CalledProcessError):
            wheelhouse.select_wheels(self.lock, self.pool)
        self.assertEqual(self.alpha.read_bytes(), b"corrupt cached wheel")

    def test_identical_filename_winner_is_reused_without_replacing_its_inode(self):
        staged = self.root / self.alpha.name
        shutil.copyfile(self.alpha, staged)
        inode = self.alpha.stat().st_ino
        wheelhouse.publish_file(staged, self.alpha, hashlib.sha256(staged.read_bytes()).hexdigest())
        self.assertEqual(self.alpha.stat().st_ino, inode)
        self.assertTrue(staged.exists())

    def test_conflicting_filename_preserves_both_existing_and_staged_bytes(self):
        staged = self.root / self.alpha.name
        shutil.copyfile(self.alpha, staged)
        expected = staged.read_bytes()
        self.alpha.write_bytes(b"other project's conflicting file")
        with self.assertRaises(FileExistsError) as refused:
            wheelhouse.publish_file(staged, self.alpha, hashlib.sha256(expected).hexdigest())
        self.assertIsInstance(refused.exception.__cause__, ValueError)
        self.assertEqual(staged.read_bytes(), expected)
        self.assertEqual(self.alpha.read_bytes(), b"other project's conflicting file")

    def test_source_archive_uses_shared_root_with_separate_pinned_identity(self):
        source_directory = self.pool
        archive = b"pinned source archive"
        declaration = self.root / "unicore-source.json"
        declaration.write_text(
            json.dumps(
                {
                    "name": "unicore",
                    "version": "0.0.1",
                    "commit": "a" * 40,
                    "url": "https://codeload.github.com/dptech-corp/Uni-Core/tar.gz/" + "a" * 40,
                    "size": len(archive),
                    "sha256": hashlib.sha256(archive).hexdigest(),
                }
            )
        )
        with patch.object(wheelhouse, "open_url", return_value=io.BytesIO(archive)) as network:
            wheelhouse.prepare_source(declaration, source_directory)
            wheelhouse.prepare_source(declaration, source_directory)
        self.assertEqual(network.call_count, 1)
        source = source_directory / ("Uni-Core-" + "a" * 40 + ".tar.gz")
        self.assertEqual(source.read_bytes(), archive)
        self.assertFalse((self.pool / "unicore-source.tar.gz").exists())
        source.write_bytes(b"conflicting source")
        with self.assertRaises(ValueError):
            wheelhouse.prepare_source(declaration, source_directory)
        self.assertEqual(source.read_bytes(), b"conflicting source")
