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

from tools.wheelhouse import requirements


class ProviderImageCommandTests(unittest.TestCase):
    def test_capture_uses_external_sets_without_old_local_dependency_directories(self):
        with ImageSource() as fixture:
            shared = fixture.root / "shared wheels"
            shutil.move(str(fixture.wheelhouse), shared)
            fixture.wheelhouse = shared
            fixture.pool_wheels = {name: shared / path.name for name, path in fixture.pool_wheels.items()}
            self.assertFalse((fixture.repository / ".work/provider-core").exists())
            result, capture = fixture.capture()
            self.assertEqual(result.returncode, 0, result.stderr)
            runtime = capture / "runtime-wheels"
            build = capture / "build-wheels"
            self.assertEqual({path.name for path in runtime.iterdir()}, {fixture.pool_wheels["runtime"].name})
            self.assertEqual({path.name for path in build.iterdir()}, {fixture.pool_wheels["build"].name})
            self.assertEqual(
                (runtime / fixture.pool_wheels["runtime"].name).read_bytes(),
                fixture.pool_wheels["runtime"].read_bytes(),
            )
            self.assertEqual(
                (build / fixture.pool_wheels["build"].name).read_bytes(),
                fixture.pool_wheels["build"].read_bytes(),
            )
            runtime_names = {path.name for path in runtime.iterdir()}
            build_names = {path.name for path in build.iterdir()}
            self.assertNotIn(fixture.pool_wheels["alternate_runtime"].name, runtime_names)
            self.assertNotIn(fixture.pool_wheels["alternate_runtime"].name, build_names)
            self.assertNotIn(fixture.pool_wheels["unrelated"].name, runtime_names)
            self.assertNotIn(fixture.pool_wheels["unrelated"].name, build_names)
            original = (runtime / fixture.pool_wheels["runtime"].name).read_bytes()
            fixture.pool_wheels["runtime"].write_bytes(b"later shared-pool mutation")
            self.assertEqual((runtime / fixture.pool_wheels["runtime"].name).read_bytes(), original)
            self.assertFalse((shared / ".complete").exists())

    def test_real_build_lock_is_admitted_by_the_image_verifier(self):
        self.assertEqual(set(requirements(Path("/input/provider-core-build.lock"))), {("setuptools", "84.0.0")})

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
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("misleading ID sha256:" + "b" * 64, result.stdout)
                self.assertEqual(record.read_bytes(), ("sha256:" + "a" * 64 + "\n").encode())

    def test_committed_capture_excludes_private_and_native_inputs_and_survives_unrelated_commit(self):
        with ImageSource() as fixture:
            (fixture.repository / "PRIVATE_CANARY").write_text("outside the image grant")
            first, before = fixture.capture()
            self.assertEqual(first.returncode, 0, first.stderr)
            captured = {str(path.relative_to(before / "context")) for path in (before / "context").rglob("*") if path.is_file()}
            self.assertIn("nmrnet_provider/process.py", captured)
            self.assertIn("tools/provider_proxy.py", captured)
            self.assertIn("tools/bound_http_proxy.py", captured)
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
                ".work/wheelhouse",
            ):
                (root / name).mkdir(parents=True, exist_ok=True)
            for name in (
                "container.sh",
                "docker_client.sh",
                "inference_native.sh",
                "provider_image.sh",
                "wheelhouse_path.sh",
                "wheelhouse_inputs.sh",
            ):
                shutil.copyfile(Path("tools") / name, root / "tools" / name)
            result = subprocess.run(
                ["bash", str(root / "tools/container.sh"), "--print", "provider/image/build"],
                env={
                    **os.environ,
                    "NMRNET_IN_CONTAINER": "0",
                    "NMRNET_WHEELHOUSE_INPUT": str(root / ".work/wheelhouse"),
                    "NMRNET_WHEELHOUSE_INPUT_ORIGIN": "command line",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = [shlex.split(line) for line in result.stdout.splitlines() if line.strip()]
            captures = [command for command in commands if "tools.wheelhouse" in command]
            self.assertEqual(len(captures), 2, result.stdout)
            (build_command,) = [command for command in commands if "--build-context" in command]
            capture = root / ".work/provider/.build.PRINT"
            for capture_command in captures:
                mounts = [
                    capture_command[index + 1] for index, argument in enumerate(capture_command[:-1]) if argument == "--mount"
                ]
                self.assertIn(f"type=bind,src={root}/.work/wheelhouse,dst=/wheelhouse,readonly", mounts)
            self.assertEqual({command[command.index("--profile") + 1] for command in captures}, {"provider-core"})
            self.assertEqual(
                {
                    argument
                    for command in captures
                    for argument in command
                    if argument.endswith(",dst=/input/requirements.lock,readonly")
                },
                {
                    f"type=bind,src={capture}/context/requirements/provider-core.lock,dst=/input/requirements.lock,readonly",
                    f"type=bind,src={capture}/context/requirements/provider-core-build.lock,dst=/input/requirements.lock,readonly",
                },
            )
            command = build_command
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertIn("--pull=false", command)
            self.assertEqual(command[command.index("--builder") + 1], "default")
            self.assertEqual(command[command.index("--progress") + 1], "plain")
            self.assertNotIn("--quiet", command)
            self.assertEqual(command[command.index("--iidfile") + 1], str(capture / "built-image-id"))
            self.assertEqual(command[-1], str(capture / "context"))
            contexts = {command[index + 1] for index, word in enumerate(command) if word == "--build-context"}
            self.assertEqual(contexts, {f"runtime-wheels={capture}/runtime-wheels", f"build-wheels={capture}/build-wheels"})
            self.assertFalse(capture.exists())
            self.assertFalse((root / ".work/provider").exists())
            self.assertNotIn("--secret", command)
            self.assertNotIn("--ssh", command)
