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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.parse import urlsplit

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

    def test_capture_freezes_only_selected_bytes(self):
        destination = self.root / "capture"
        destination.mkdir()
        identity = wheelhouse.capture(self.lock, None, self.pool, destination)
        self.assertEqual({path.name for path in destination.iterdir()}, {self.alpha.name})
        self.assertEqual({self.alpha.name}, set(identity["files"]))
        original = self.alpha.read_bytes()
        self.alpha.write_bytes(b"later pool modification")
        self.assertEqual((destination / self.alpha.name).read_bytes(), original)

    def test_complete_pool_skips_acquisition_and_preserves_all_cached_files(self):
        before = {path.name: path.read_bytes() for path in self.pool.iterdir()}
        run = subprocess.run

        def offline_only(command, **kwargs):
            self.assertIn("--no-index", command, "A complete pool must not query a package index")
            return run(command, **kwargs)

        with patch.object(wheelhouse.subprocess, "run", side_effect=offline_only):
            for _ in range(2):
                identity = wheelhouse.populate_pool(self.lock, self.pool, inference=True)
                self.assertEqual(set(identity["files"]), {self.alpha.name})
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.pool.iterdir()})

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
        self.assertFalse((self.pool / wheelhouse.SOURCE_NAME).exists())
        destination = self.root / "source-bearing capture"
        destination.mkdir()
        identity = wheelhouse.capture(self.lock, declaration, self.pool, destination)
        self.assertEqual({path.name for path in destination.iterdir()}, {self.alpha.name, wheelhouse.SOURCE_NAME})
        self.assertEqual({self.alpha.name, wheelhouse.SOURCE_NAME}, set(identity["files"]))
        self.assertEqual((destination / wheelhouse.SOURCE_NAME).read_bytes(), archive)
        self.assertEqual(hashlib.sha256(archive).hexdigest(), identity["files"][wheelhouse.SOURCE_NAME])
        self.assertEqual(wheelhouse.digest(declaration), identity["source_declaration_sha256"])
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
                destination = Path(command[command.index("--dest") + 1]) / incoming.name
                # Pip unlinks a bad-hash destination before downloading again.
                if destination.is_symlink():
                    destination.unlink()
                shutil.copyfile(incoming, destination)
                return subprocess.CompletedProcess(command, 0)
            return run(command, **kwargs)

        before = {path.name: path.read_bytes() for path in self.pool.iterdir()}
        with patch.object(wheelhouse.subprocess, "run", side_effect=download) as calls:
            wheelhouse.populate_pool(lock, self.pool, inference=False)
            inode = (self.pool / incoming.name).stat().st_ino
            wheelhouse.populate_pool(lock, self.pool, inference=False)
            self.assertEqual(sum("download" in call.args[0] for call in calls.call_args_list), 1)
            self.assertEqual((self.pool / incoming.name).stat().st_ino, inode)
            self.assertEqual((self.pool / incoming.name).read_bytes(), incoming.read_bytes())
            (self.pool / incoming.name).write_bytes(b"conflicting pooled bytes")
            with self.assertRaises(FileExistsError):
                wheelhouse.populate_pool(lock, self.pool, inference=False)
            self.assertEqual((self.pool / incoming.name).read_bytes(), b"conflicting pooled bytes")
        for name, content in before.items():
            self.assertEqual((self.pool / name).read_bytes(), content)
        self.assertEqual([], list(self.pool.glob(".nmrnet-wheel-download-*")))
        self.assertFalse(any((self.pool / profile).exists() for profile in ("inference", "preparation", "provider-core")))

    def test_partial_lock_progress_is_published_and_reused_after_retry(self):
        incoming = self.root / "partial incoming"
        incoming.mkdir()
        cached = self.alpha
        cached_bytes, cached_inode = cached.read_bytes(), cached.stat().st_ino
        alpha = wheel(incoming, "pool-gamma", "1.0", payload=b"new locked gamma")
        beta = wheel(incoming, "pool-beta", "3.0", payload=b"new locked beta")
        lock = locked(
            self.root / "partial.lock",
            [
                ("pool-alpha", "1.0", [cached]),
                ("pool-gamma", "1.0", [alpha]),
                ("pool-beta", "3.0", [beta]),
            ],
        )
        real_run = subprocess.run
        file_gets = {cached.name: 0, alpha.name: 0, beta.name: 0}
        fail_beta_once = {"value": True}

        class LocalIndex(BaseHTTPRequestHandler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if path.startswith("/simple/"):
                    package = path.strip("/").split("/")[-1]
                    candidate = {"pool-alpha": cached, "pool-gamma": alpha, "pool-beta": beta}[package]
                    checksum = hashlib.sha256(candidate.read_bytes()).hexdigest()
                    body = (
                        f'<a href="http://127.0.0.1:{self.server.server_port}/files/{candidate.name}'
                        f'#sha256={checksum}">{candidate.name}</a>'
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Cache-Control", "max-age=3600")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                filename = path.removeprefix("/files/")
                candidate = {cached.name: cached, alpha.name: alpha, beta.name: beta}.get(filename)
                if candidate is None:
                    self.send_error(404)
                    return
                file_gets[filename] += 1
                if filename == beta.name and fail_beta_once["value"]:
                    fail_beta_once["value"] = False
                    self.send_error(503)
                    return
                body = candidate.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Cache-Control", "max-age=3600")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), LocalIndex)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def local_pip(command, **kwargs):
            if "download" not in command:
                return real_run(command, **kwargs)
            self.assertIn("--cache-dir", command)
            self.assertEqual(str(self.pool / ".pip-cache"), command[command.index("--cache-dir") + 1])
            self.assertNotIn("--no-cache-dir", command)
            rewritten = list(command)
            index = rewritten.index("--index-url")
            rewritten[index + 1] = f"http://127.0.0.1:{server.server_port}/simple"
            rewritten[index + 2 : index + 2] = ["--trusted-host", "127.0.0.1", "--retries", "0"]
            return real_run(rewritten, **kwargs)

        with patch.object(wheelhouse.subprocess, "run", side_effect=local_pip):
            with self.assertRaises(subprocess.CalledProcessError):
                wheelhouse.populate_pool(lock, self.pool, inference=False)
            self.assertEqual({cached.name: 0, alpha.name: 1, beta.name: 1}, file_gets)
            self.assertFalse((self.pool / alpha.name).exists())
            self.assertFalse((self.pool / beta.name).exists())
            self.assertEqual([], list(self.pool.glob(".nmrnet-wheel-download-*")))

            wheelhouse.populate_pool(lock, self.pool, inference=False)

        self.assertEqual({cached.name: 0, alpha.name: 1, beta.name: 2}, file_gets)
        self.assertEqual((cached.read_bytes(), cached.stat().st_ino), (cached_bytes, cached_inode))
        self.assertEqual(alpha.read_bytes(), (self.pool / alpha.name).read_bytes())
        self.assertEqual(beta.read_bytes(), (self.pool / beta.name).read_bytes())
