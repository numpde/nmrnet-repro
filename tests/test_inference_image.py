"""Verify pinned source admission before setup execution and image lane authority."""

import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import re
import shutil
import signal
import time
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
        (self.root / "tools/inference_native.sh").write_bytes(Path("tools/inference_native.sh").read_bytes())
        (self.root / "tools/inference_inputs.sh").write_bytes(Path("tools/inference_inputs.sh").read_bytes())
        (self.root / "requirements").mkdir()
        for name in ("inference.lock", "unicore-source.json"):
            (self.root / "requirements" / name).write_text("fixture\n")
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
        self.assertEqual(args[args.index("--builder") + 1], "default")
        self.assertIn("--pull=false", args)
        self.assertIn("wheelhouse=" + str(self.root / ".work/inference/.build.PRINT/wheelhouse"), args)
        self.assertNotIn("--ssh", args)
        self.assertNotIn("--secret", args)
        self.assertFalse((self.root / ".work/inference").exists())

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

    def test_timeout_requires_positive_finite_seconds_without_dispatch(self):
        for budget in ("", "0", "-1", "nan", "inf", "1e3", "1;echo private"):
            with self.subTest(budget=budget):
                result = subprocess.run(["bash", str(self.root / "tools/inference_image.sh"),
                                         "--print", "--timeout", budget, "import"],
                                        text=True, capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("positive finite seconds", result.stderr)

    def test_nul_cannot_disguise_a_saved_image_identity(self):
        (self.root / ".work/inference").mkdir()
        (self.root / ".work/inference/image-id").write_bytes(b"sha256:" + b"b" * 64 + b"\0")
        result = self.command("import")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")


class NativeClientTests(unittest.TestCase):
    def test_repeated_native_calls_do_not_exhaust_the_contained_pid_budget(self):
        helper = Path("tools/inference_native.sh").resolve()
        result = subprocess.run(["bash", "-c", 'source "$1"; for ((index=0; index<40; index++)); do inference_native repeated 5 discard true || exit "$?"; done',
                                 "bash", str(helper)], text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_deadline_retires_term_resistant_writer_and_rejects_late_success(self):
        helper = Path("tools/inference_native.sh").resolve()
        for mode in ("held-pipe", "late-success"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                receipt = Path(directory) / "child"
                body = 'trap "" TERM; printf "%s\\n" "$BASHPID" > "$1"; sleep 0.3; exit 0'
                if mode == "held-pipe":
                    body = 'bash -c \'trap "" TERM; printf "%s\\n" "$BASHPID" > "$1"; sleep 60\' bash "$1" & wait'
                command = ["bash", "-c", 'source "$1"; inference_native deadline 0.1 discard bash -c "$2" bash "$3"',
                           "bash", str(helper), body, str(receipt)]
                result = subprocess.run(command, text=True, capture_output=True, timeout=2)
                self.assertEqual(result.returncode, 124, result.stderr)
                self.assertIn("deadline elapsed", result.stderr)
                child = receipt.read_text().strip()
                try:
                    state = Path(f"/proc/{child}/stat").read_text().split()[2]
                except (FileNotFoundError, ProcessLookupError):
                    state = "retired"
                self.assertIn(state, {"Z", "retired"})
                match = re.search(r"retained at (/tmp/nmrnet-inference-native\.[A-Za-z0-9]+/command\.log)", result.stderr)
                self.assertIsNotNone(match, result.stderr)
                shutil.rmtree(Path(match.group(1)).parent)

    def test_rejected_identity_does_not_claim_that_successful_client_failed(self):
        helper = Path("tools/inference_native.sh").resolve()
        result = subprocess.run(["bash", "-c", 'source "$1"; inference_native identity 5 image-id printf "mutable:tag\\n"',
                                 "bash", str(helper)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("identity output", result.stderr)
        self.assertNotIn("native client exited", result.stderr)
        match = re.search(r"retained at (/tmp/nmrnet-inference-native\.[A-Za-z0-9]+/command\.log)", result.stderr)
        self.assertIsNotNone(match, result.stderr)
        shutil.rmtree(Path(match.group(1)).parent)

    def test_cancellation_preserves_signal_status_and_retires_owned_descendant(self):
        helper = Path("tools/inference_native.sh").resolve()
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as directory:
                receipt = Path(directory) / "child"
                command = ["bash", "-c", 'source "$1"; inference_native cancellation 5 discard bash -c "$2" bash "$3"',
                           "bash", str(helper), 'sleep 60 & printf "%s\\n" "$!" > "$1"; wait', str(receipt)]
                process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, start_new_session=True)
                try:
                    deadline = time.monotonic() + 2
                    while not receipt.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(receipt.exists(), "The native descendant did not start before cancellation")
                    process.send_signal(signum)
                    stdout, stderr = process.communicate(timeout=2)
                    self.assertEqual(process.returncode, 128 + signum, stderr)
                    self.assertEqual(stdout, "")
                    child = receipt.read_text().strip()
                    try:
                        state = Path(f"/proc/{child}/stat").read_text().split()[2]
                    except FileNotFoundError:
                        state = "retired"
                    self.assertIn(state, {"Z", "retired"})
                    match = re.search(r"retained at (/tmp/nmrnet-inference-native\.[A-Za-z0-9]+/command\.log)", stderr)
                    self.assertIsNotNone(match, stderr)
                    shutil.rmtree(Path(match.group(1)).parent)
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate(timeout=2)

    def test_failed_client_retains_private_bounded_evidence_and_exact_status(self):
        helper = Path("tools/inference_native.sh").resolve()
        result = subprocess.run(["bash", "-c", 'source "$1"; inference_native probe 5 discard python3 -c "$2"',
                                 "bash", str(helper),
                                 "import sys; sys.stdout.write('private-marker' + 'x' * 8388609); sys.exit(7)"],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertNotIn("private-marker", result.stdout + result.stderr)
        match = re.search(r"retained at (/tmp/nmrnet-inference-native\.[A-Za-z0-9]+/command\.log)", result.stderr)
        self.assertIsNotNone(match, result.stderr)
        log = Path(match.group(1))
        self.addCleanup(shutil.rmtree, log.parent)
        self.assertEqual(log.stat().st_size, 8388608)
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(log.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
