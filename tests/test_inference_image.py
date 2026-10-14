"""Verify pinned source admission before setup execution and image lane authority."""

import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools import build_unicore


class SourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.archive = self.root / "source.tar.gz"
        self.declaration = self.root / "source.json"
        self.prefix = "Uni-Core-" + "a" * 40

    def write_archive(self, members):
        with tarfile.open(self.archive, "w:gz") as archive:
            for name, kind, body in members:
                info = tarfile.TarInfo(name)
                info.type = kind
                info.size = len(body) if kind == tarfile.REGTYPE else 0
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    info.linkname = "../../outside"
                archive.addfile(info, io.BytesIO(body))
        body = self.archive.read_bytes()
        self.declaration.write_text(json.dumps({
            "name": "unicore", "version": "0.0.1", "commit": "a" * 40,
            "url": "https://codeload.github.com/dptech-corp/Uni-Core/tar.gz/" + "a" * 40,
            "size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
        }))

    def build(self):
        build_unicore.build(self.archive, self.declaration)

    def test_verified_source_builds_without_network_or_isolated_dependencies(self):
        self.write_archive([(self.prefix + "/setup.py", tarfile.REGTYPE, b"pinned setup")])
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            self.assertEqual((Path(command[-1]) / "setup.py").read_bytes(), b"pinned setup")
        with patch.object(build_unicore.subprocess, "run", side_effect=run):
            self.build()
        self.assertEqual(len(commands), 1)
        for flag in ("--no-index", "--no-deps", "--no-build-isolation", "--no-cache-dir"):
            self.assertIn(flag, commands[0])

    def test_hash_mismatch_is_rejected_before_archive_processing(self):
        self.write_archive([(self.prefix + "/setup.py", tarfile.REGTYPE, b"setup")])
        self.archive.write_bytes(b"changed")
        with patch.object(build_unicore.tarfile, "open", side_effect=AssertionError("archive parsed")), \
             patch.object(build_unicore.subprocess, "run", side_effect=AssertionError("setup ran")):
            with self.assertRaisesRegex(ValueError, "hash|size"):
                self.build()

    def test_unsafe_paths_links_duplicates_and_wrong_root_never_execute(self):
        bad_members = [
            [(self.prefix + "/../outside", tarfile.REGTYPE, b"bad")],
            [("/absolute", tarfile.REGTYPE, b"bad")],
            [("another-root/setup.py", tarfile.REGTYPE, b"bad")],
            [(self.prefix + "/link", tarfile.SYMTYPE, b"")],
            [(self.prefix + "/link", tarfile.LNKTYPE, b"")],
            [(self.prefix + "/pipe", tarfile.FIFOTYPE, b"")],
            [(self.prefix + "/same", tarfile.REGTYPE, b"a"),
             (self.prefix + "/same", tarfile.REGTYPE, b"b")],
        ]
        for members in bad_members:
            with self.subTest(members=members):
                self.write_archive([(self.prefix + "/setup.py", tarfile.REGTYPE, b"setup")] + members)
                with patch.object(build_unicore.subprocess, "run", side_effect=AssertionError("setup ran")):
                    with self.assertRaises(ValueError):
                        self.build()
                self.assertFalse((self.root / "outside").exists())

    def test_expanded_archive_and_member_count_are_bounded(self):
        self.write_archive([(self.prefix + "/setup.py", tarfile.REGTYPE, b"x" * 4096)])
        with patch.object(build_unicore, "MAX_ARCHIVE_BYTES", 1024), \
             patch.object(build_unicore.subprocess, "run", side_effect=AssertionError("setup ran")):
            with self.assertRaisesRegex(ValueError, "limit"):
                self.build()
        with patch.object(build_unicore, "MAX_MEMBERS", 0):
            with self.assertRaisesRegex(ValueError, "members"):
                self.build()

    def test_missing_setup_is_rejected(self):
        for members in ([(self.prefix + "/README.md", tarfile.REGTYPE, b"readme")],
                        [(self.prefix + "/setup.py", tarfile.DIRTYPE, b"")]):
            self.write_archive(members)
            with patch.object(build_unicore.subprocess, "run", side_effect=AssertionError("setup ran")):
                with self.assertRaisesRegex(ValueError, "setup.py"):
                    self.build()


class ImageLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tools").mkdir()
        (self.root / "tools/inference_image.sh").write_bytes(Path("tools/inference_image.sh").read_bytes())
        (self.root / ".work/wheelhouse/cpu").mkdir(parents=True)
        (self.root / ".work/wheelhouse/cpu/.complete").write_text("{}")
        self.git_function = ('() { case "$*" in '
                             '*"ls-files --stage"*) echo "160000 ' + "a" * 40 + ' 0 NMRNetplusplus";; '
                             '*"rev-parse HEAD"*) echo "' + "a" * 40 + '";; esac; }')

    def command(self, action):
        return subprocess.run(["bash", str(self.root / "tools/inference_image.sh"), "--print", action],
                              env={**os.environ, "BASH_FUNC_git%%": self.git_function},
                              text=True, capture_output=True)

    def test_build_is_offline_with_explicit_wheelhouse_context(self):
        result = self.command("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = shlex.split(result.stdout.splitlines()[-1])
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--pull=false", args)
        self.assertIn("wheelhouse=" + str(self.root / ".work/wheelhouse/cpu"), args)
        self.assertNotIn("--ssh", args)
        self.assertNotIn("--secret", args)

    def test_missing_or_symlinked_wheelhouse_is_rejected(self):
        (self.root / ".work/wheelhouse/cpu/.complete").unlink()
        self.assertNotEqual(self.command("build").returncode, 0)
        (self.root / ".work/wheelhouse/cpu").rmdir()
        (self.root / ".work/wheelhouse/cpu").symlink_to(self.root / "elsewhere")
        self.assertNotEqual(self.command("build").returncode, 0)

    def test_changed_source_and_git_inspection_failure_reject_build(self):
        original = self.git_function
        for result in ('echo " M uninmr/infer.py"', "return 1"):
            self.git_function = original.replace(';; esac;', ';; *"status --porcelain"*) ' + result + ';; esac;')
            command = self.command("build")
            self.assertNotEqual(command.returncode, 0)
            self.assertEqual(command.stdout, "")

    def test_import_uses_recorded_immutable_image_offline_without_mounts(self):
        (self.root / ".work/inference").mkdir()
        (self.root / ".work/inference/image-id").write_text("sha256:" + "b" * 64 + "\n")
        result = self.command("import")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = shlex.split(result.stdout)
        self.assertIn("sha256:" + "b" * 64, args)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertIn("--read-only", args)
        self.assertIn("NMRNET_IN_CONTAINER=1", args)
        self.assertIn("--pull", args)
        self.assertNotIn("--mount", args)
        self.assertNotIn("--gpus", args)
        self.assertIn("no-new-privileges:true", args)
        for flag in ("--memory", "--memory-swap", "--cpus", "--pids-limit"):
            self.assertIn(flag, args)

    def test_invalid_image_identity_is_rejected(self):
        (self.root / ".work/inference").mkdir()
        (self.root / ".work/inference/image-id").write_text("mutable:tag\n")
        result = self.command("import")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("image", result.stderr)


if __name__ == "__main__":
    unittest.main()
