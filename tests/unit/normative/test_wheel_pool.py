"""A common pool supplies lock-selected wheels without owning other projects' files."""

from tools.container_guard import require_container

require_container()

import hashlib
import io
import json
import shutil
import subprocess
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
        (self.pool / ".complete").write_bytes(b"another project's receipt")
        before = {path.name: path.read_bytes() for path in self.pool.iterdir()}
        identity = wheelhouse.select_wheels(self.lock, self.pool)
        self.assertEqual(identity["files"], {self.alpha.name: hashlib.sha256(self.alpha.read_bytes()).hexdigest()})
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.pool.iterdir()})
        second = locked(self.root / "beta.lock", [("pool-beta", "2.0", [self.beta])])
        self.assertEqual(set(wheelhouse.select_wheels(second, self.pool)["files"]), {self.beta.name})

    def test_capture_freezes_only_selected_bytes_and_retains_exact_private_receipt(self):
        destination = self.root / "capture"
        destination.mkdir()
        wheelhouse.capture(self.lock, None, self.pool, destination, None)
        self.assertEqual({path.name for path in destination.iterdir()}, {self.alpha.name, ".complete"})
        original = self.alpha.read_bytes()
        self.alpha.write_bytes(b"later pool modification")
        wheelhouse.check(self.lock, None, destination)
        self.assertEqual((destination / self.alpha.name).read_bytes(), original)

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

    def test_source_archive_uses_separate_repository_artifact_cache(self):
        source_directory = self.root / "downloads"
        source_directory.mkdir()
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
        self.assertFalse((self.pool / wheelhouse.SOURCE_NAME).exists())
        destination = self.root / "source-bearing capture"
        destination.mkdir()
        wheelhouse.capture(self.lock, declaration, self.pool, destination, source_directory)
        self.assertEqual({path.name for path in destination.iterdir()}, {self.alpha.name, wheelhouse.SOURCE_NAME, ".complete"})
        self.assertEqual((destination / wheelhouse.SOURCE_NAME).read_bytes(), archive)
        wheelhouse.check(self.lock, declaration, destination)
        source.write_bytes(b"conflicting source")
        with self.assertRaises(ValueError):
            wheelhouse.prepare_source(declaration, source_directory)
        self.assertEqual(source.read_bytes(), b"conflicting source")

    def test_per_file_publication_preserves_existing_pool_files(self):
        source = self.root / "incoming"
        source.mkdir()
        incoming = wheel(source, "pool-gamma", "3.0")
        lock = locked(self.root / "gamma.lock", [("pool-gamma", "3.0", [incoming])])
        run = subprocess.run

        def download(command, **kwargs):
            if "download" in command:
                self.assertIn(str(self.pool), command)
                shutil.copyfile(incoming, Path(command[command.index("--dest") + 1]) / incoming.name)
                return subprocess.CompletedProcess(command, 0)
            return run(command, **kwargs)

        before = {path.name: path.read_bytes() for path in self.pool.iterdir()}
        with patch.object(wheelhouse.subprocess, "run", side_effect=download):
            wheelhouse.populate_pool(lock, self.pool, inference=False)
            inode = (self.pool / incoming.name).stat().st_ino
            wheelhouse.populate_pool(lock, self.pool, inference=False)
            self.assertEqual((self.pool / incoming.name).stat().st_ino, inode)
            self.assertEqual((self.pool / incoming.name).read_bytes(), incoming.read_bytes())
            (self.pool / incoming.name).write_bytes(b"conflicting pooled bytes")
            with self.assertRaises(FileExistsError):
                wheelhouse.populate_pool(lock, self.pool, inference=False)
            self.assertEqual((self.pool / incoming.name).read_bytes(), b"conflicting pooled bytes")
        for name, content in before.items():
            self.assertEqual((self.pool / name).read_bytes(), content)
        self.assertFalse((self.pool / ".complete").exists())
        self.assertTrue(all(path.is_file() for path in self.pool.iterdir()))
