"""The preparation launcher grants offline reads and one dedicated output mount."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


class BaselineLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name in ("tools", "artifacts", ".work/inference", ".work/prepared/data", ".work/prepared/weight"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        (self.root / "tools/baseline.sh").write_bytes(Path("tools/baseline.sh").read_bytes())
        for name in ("tools/artifacts.py", "tools/baseline_prepare.py", "artifacts/release.json",
                     "artifacts/data.selection.json", "artifacts/weight.selection.json",
                     ".work/prepared/data/provenance.json", ".work/prepared/weight/provenance.json"):
            (self.root / name).write_text("{}")
        (self.root / ".work/inference/image-id").write_text("sha256:" + "a" * 64 + "\n")

    def command(self, action="prepare"):
        return subprocess.run(["bash", str(self.root / "tools/baseline.sh"), "--print", action],
                              env={**os.environ, "DOCKER_HOST": "tcp://ambient.invalid:2375",
                                   "DOCKER_CONTEXT": "ambient", "ARCHIVE": "ignored.zip"},
                              text=True, capture_output=True)

    def test_prepare_is_offline_bounded_and_uses_recorded_image(self):
        result = self.command()
        self.assertEqual(result.returncode, 0, result.stderr)
        args = shlex.split(result.stdout)
        self.assertEqual(args[args.index("--network") + 1], "none")
        self.assertEqual(args[args.index("--pull") + 1], "never")
        self.assertEqual(args[args.index("--host") + 1], "unix:///var/run/docker.sock")
        self.assertIn("--read-only", args)
        self.assertIn("no-new-privileges:true", args)
        self.assertIn("sha256:" + "a" * 64, args)
        self.assertEqual(args[args.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
        self.assertEqual(args[args.index("--cap-drop") + 1], "ALL")
        for flag in ("--cpus", "--memory", "--memory-swap", "--pids-limit", "--tmpfs"):
            self.assertIn(flag, args)
        self.assertNotIn("--gpus", args)
        self.assertNotIn("--device", args)
        self.assertNotIn("--privileged", args)
        self.assertFalse((self.root / ".work/baseline").exists())

    def test_only_declared_inputs_and_dedicated_output_are_mounted(self):
        args = shlex.split(self.command().stdout)
        mounts = [args[index + 1] for index, item in enumerate(args) if item == "--mount"]
        expected = {
            "tools/artifacts.py": "/workspace/tools/artifacts.py",
            "tools/baseline_prepare.py": "/workspace/tools/baseline_prepare.py",
            "artifacts/release.json": "/input/release.json",
            "artifacts/data.selection.json": "/input/data.selection.json",
            "artifacts/weight.selection.json": "/input/weight.selection.json",
            ".work/prepared/data": "/prepared/data",
            ".work/prepared/weight": "/prepared/weight",
        }
        self.assertEqual(set(mounts), {
            f"type=bind,src={self.root / source},dst={destination},readonly"
            for source, destination in expected.items()
        } | {f"type=bind,src={self.root / '.work/baseline'},dst=/baseline"})
        self.assertEqual(args[args.index("--workdir") + 1], "/workspace")
        self.assertIn("tools.baseline_prepare", args)
        for flag, value in (("--prepared-root", "/prepared"), ("--declaration", "/input/release.json"),
                            ("--data-selection", "/input/data.selection.json"),
                            ("--dictionary-selection", "/input/weight.selection.json"),
                            ("--output", "/baseline/prepared"), ("--count", "2"),
                            ("--max-atoms", "128"), ("--max-tokens", "130")):
            self.assertEqual(args[args.index(flag) + 1], value)

    def test_missing_and_mutable_image_identity_reject_before_engine_access(self):
        image = self.root / ".work/inference/image-id"
        image.unlink()
        self.assertNotEqual(self.command().returncode, 0)
        image.write_text("mutable:tag\n")
        self.assertNotEqual(self.command().returncode, 0)

    def test_symlinked_input_or_output_parent_rejects(self):
        for name in ("artifacts/data.selection.json", ".work/inference/image-id",
                     ".work/prepared/data/provenance.json", ".work/prepared/data", ".work/baseline"):
            with self.subTest(name=name):
                path = self.root / name
                saved = path.with_name(path.name + ".saved")
                if path.exists():
                    path.rename(saved)
                path.symlink_to(saved)
                result = self.command()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                path.unlink()
                if saved.exists():
                    saved.rename(path)

    def test_missing_prepared_receipt_rejects(self):
        (self.root / ".work/prepared/data/provenance.json").unlink()
        result = self.command()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("provenance.json", result.stderr)

    def test_existing_output_is_preserved_and_rejected(self):
        output = self.root / ".work/baseline/prepared"
        output.mkdir(parents=True)
        (output / "keep").write_text("existing")
        result = self.command()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((output / "keep").read_text(), "existing")
        self.assertEqual(result.stdout, "")

    def test_run_lane_is_not_available(self):
        self.assertNotEqual(self.command("run").returncode, 0)


if __name__ == "__main__":
    unittest.main()
