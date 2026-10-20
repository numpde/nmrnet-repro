"""Controller build commands carry frozen inputs and no runtime credentials."""

from tools.container_guard import require_container

require_container()

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit.provider_image_ports import ImageSource


class ProviderImageCommandTests(unittest.TestCase):
    def test_committed_capture_excludes_private_and_native_inputs_and_survives_unrelated_commit(self):
        with ImageSource() as fixture:
            (fixture.repository / "PRIVATE_CANARY").write_text("outside the image grant")
            first, before = fixture.capture()
            self.assertEqual(first.returncode, 0, first.stderr)
            captured = {str(path.relative_to(before / "context")) for path in (before / "context").rglob("*") if path.is_file()}
            self.assertIn("nmrnet_provider/process.py", captured)
            self.assertIn("packages/provider-core/src/provider_core/__init__.py", captured)
            self.assertNotIn("PRIVATE_CANARY", captured)
            for name in ("worker_main.py", "geometry_worker.py", "smiles_worker.py"):
                self.assertNotIn("nmrnet_provider/" + name, captured)
            fixture.git("add", "--", "PRIVATE_CANARY")
            fixture.git("commit", "--quiet", "-m", "Unrelated committed canary", "--", "PRIVATE_CANARY")
            second, after = fixture.capture()
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual((before / "inputs").read_bytes(), (after / "inputs").read_bytes())

    def test_build_target_uses_existing_offline_launcher(self):
        result = subprocess.run(
            ["make", "--dry-run", "--no-print-directory", "provider/image/build"], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "bash tools/container.sh provider/image/build")

    def test_build_receives_only_frozen_contexts_and_never_pulls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "tools",
                "containers/provider",
                "requirements",
                "packages/provider-core",
                "nmrnet_provider",
                "nmrnet_inference",
                ".work/provider-core/wheelhouse",
                ".work/wheelhouse/cpu",
            ):
                (root / name).mkdir(parents=True, exist_ok=True)
            for name in ("container.sh", "docker_client.sh", "inference_native.sh", "provider_image.sh"):
                shutil.copyfile(Path("tools") / name, root / "tools" / name)
            result = subprocess.run(
                ["bash", str(root / "tools/container.sh"), "--print", "provider/image/build"],
                env={**os.environ, "NMRNET_IN_CONTAINER": "0"},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            command = shlex.split(result.stdout)
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertIn("--pull=false", command)
            self.assertEqual(command[command.index("--builder") + 1], "default")
            capture = root / ".work/provider/.build.PRINT"
            self.assertEqual(command[-1], str(capture / "context"))
            contexts = {command[index + 1] for index, word in enumerate(command) if word == "--build-context"}
            self.assertEqual(contexts, {f"runtime-wheels={capture}/runtime-wheels", f"build-wheels={capture}/build-wheels"})
            self.assertFalse(capture.exists())
            self.assertFalse((root / ".work/provider").exists())
            self.assertNotIn("--secret", command)
            self.assertNotIn("--ssh", command)
