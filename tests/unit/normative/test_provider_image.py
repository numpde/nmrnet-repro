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
    def test_source_capture_keeps_wheels_in_the_shared_pool(self):
        with ImageSource() as fixture:
            shared = fixture.root / "shared wheels"
            shutil.move(str(fixture.wheelhouse), shared)
            fixture.wheelhouse = shared
            fixture.pool_wheels = {name: shared / path.name for name, path in fixture.pool_wheels.items()}
            self.assertFalse((fixture.repository / ".work/provider-core").exists())
            result, capture = fixture.capture()
            self.assertEqual(0, result.returncode, msg=result.stderr)
            runtime = capture / "runtime-wheels"
            self.assertFalse((capture / "build-wheels").exists())
            self.assertFalse(runtime.exists())
            self.assertFalse((capture / "wheelhouse").exists())
            self.assertTrue(fixture.pool_wheels["runtime"].is_file())
            self.assertFalse((shared / ".complete").exists())

    def test_progress_is_not_identity_and_both_complete_id_forms_publish(self):
        for form in ("plain", "newline"):
            with self.subTest(form=form), ImageSource() as fixture:
                directory = fixture.repository / ".work/provider"
                directory.mkdir()
                record = directory / "image-id"
                record.write_bytes(b"previous image evidence\n")
                result = subprocess.run(
                    ["bash", "tests/fixtures/provider_image_build.sh", str(fixture.repository), form],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                self.assertEqual(0, result.returncode, msg=result.stderr)
                self.assertIn("misleading ID sha256:" + "b" * 64, result.stdout)
                self.assertEqual(record.read_bytes(), ("sha256:" + "a" * 64 + "\n").encode())

    def test_committed_capture_excludes_private_and_native_inputs_and_survives_unrelated_commit(self):
        with ImageSource() as fixture:
            (fixture.repository / "PRIVATE_CANARY").write_text("outside the image grant")
            first, before = fixture.capture()
            self.assertEqual(0, first.returncode, msg=first.stderr)
            captured = {str(path.relative_to(before / "context")) for path in (before / "context").rglob("*") if path.is_file()}
            self.assertIn("nmrnet_provider/process.py", captured)
            self.assertNotIn("tools/provider_proxy.py", captured)
            self.assertNotIn("tools/bound_http_proxy.py", captured)
            self.assertFalse(any(name.startswith("packages/provider-core/") for name in captured))
            self.assertNotIn("PRIVATE_CANARY", captured)
            for name in ("worker_main.py", "geometry_worker.py", "smiles_worker.py"):
                self.assertNotIn("nmrnet_provider/" + name, captured)
            fixture.git("add", "--", "PRIVATE_CANARY")
            fixture.git("commit", "--quiet", "-m", "Unrelated committed canary", "--", "PRIVATE_CANARY")
            second, after = fixture.capture()
            self.assertEqual(0, second.returncode, msg=second.stderr)
            self.assertEqual((before / "inputs").read_bytes(), (after / "inputs").read_bytes())

    def test_build_target_uses_existing_offline_launcher(self):
        makefile = Path("Makefile").read_text()
        self.assertRegex(makefile, r"(?m)^provider/image/build:\n\tbash tools/container\.sh \$@$")

    def test_build_receives_only_frozen_contexts_and_never_pulls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (
                "tools",
                "containers/provider",
                "requirements",
                "nmrnet_provider",
                "nmrnet_inference",
                ".work/wheelhouse",
            ):
                (root / name).mkdir(parents=True, exist_ok=True)
            for name in (
                "container.sh",
                "docker_client.sh",
                "inference_native.sh",
                "provider_image.sh",
                "core_image.sh",
                "wheelhouse_path.sh",
            ):
                shutil.copyfile(Path("tools") / name, root / "tools" / name)
            result = subprocess.run(
                ["bash", str(root / "tools/container.sh"), "--print", "provider/image/build"],
                env={
                    **os.environ,
                    "NMRNET_IN_CONTAINER": "0",
                    "NMRNET_DOCKER_CONTEXT_INPUT": "test-local",
                    "NMRNET_DOCKER_CONTEXT_INPUT_ORIGIN": "command line",
                    "NMRNET_DOCKER_ENDPOINT_INPUT": "unix:///var/run/docker.sock",
                    "NMRNET_WHEELHOUSE_INPUT": str(root / ".work/wheelhouse"),
                    "NMRNET_WHEELHOUSE_INPUT_ORIGIN": "command line",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            commands = [shlex.split(line) for line in result.stdout.splitlines() if line.strip()]
            captures = [command for command in commands if "tools.wheelhouse" in command]
            self.assertEqual(captures, [], msg=result.stdout)
            (build_command,) = [command for command in commands if "build" in command]
            capture = root / ".work/provider/.build.PRINT"
            command = build_command
            self.assertEqual("none", command[command.index("--network") + 1])
            self.assertIn("--pull=false", command)
            self.assertEqual("default", command[command.index("--builder") + 1])
            self.assertEqual("plain", command[command.index("--progress") + 1])
            self.assertNotIn("--quiet", command)
            self.assertEqual(command[command.index("--iidfile") + 1], str(capture / "built-image-id"))
            self.assertEqual("-", command[-1])
            self.assertNotIn("--build-context", command)
            self.assertFalse(capture.exists())
            self.assertFalse((root / ".work/provider").exists())
            self.assertNotIn("--secret", command)
            self.assertNotIn("--ssh", command)
