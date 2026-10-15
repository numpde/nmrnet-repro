"""Check tool admission and the maintained launchers without network or builds."""

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULES = ("artifacts", "bound_http_proxy", "build_unicore", "package_lock", "probe", "wheelhouse")


class GuardedToolingTests(unittest.TestCase):
    def test_plain_file_entrypoints_reject_without_package_search_path(self):
        """Unsupported file invocation must stop, even before guard resolution.

        These fresh processes remain inside the test container. Importable-path
        tests separately establish the shared guard's diagnostic contract.
        """
        environment = dict(os.environ)
        environment.pop("NMRNET_IN_CONTAINER", None)
        environment.pop("PYTHONPATH", None)
        with tempfile.TemporaryDirectory() as directory:
            for path in sorted(Path("tools").glob("*.py")):
                # The shared guard defines admission; importing it owns no work.
                if path.name == "container_guard.py":
                    continue
                with self.subTest(tool=path.name):
                    result = subprocess.run(
                        [sys.executable, "-B", str(path.resolve())],
                        cwd=directory,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual("", result.stdout)
                    self.assertEqual([], list(Path(directory).iterdir()))

    def test_missing_or_invalid_marker_blocks_import_and_entrypoints(self):
        with tempfile.TemporaryDirectory() as directory:
            for module in MODULES:
                invocations = (
                    ["-c", f"import tools.{module}"],
                    ["-m", f"tools.{module}", "--help"],
                    [str(Path("tools", module + ".py").resolve()), "--help"],
                )
                for marker in (None, "0"):
                    environment = {**os.environ, "PYTHONPATH": str(Path.cwd())}
                    environment.pop("NMRNET_IN_CONTAINER", None)
                    if marker is not None:
                        environment["NMRNET_IN_CONTAINER"] = marker
                    for invocation in invocations:
                        with self.subTest(module=module, marker=marker, invocation=invocation):
                            result = subprocess.run(
                                [sys.executable, "-B", *invocation],
                                cwd=directory,
                                env=environment,
                                text=True,
                                capture_output=True,
                                check=False,
                            )
                            self.assertNotEqual(result.returncode, 0)
                            self.assertIn("maintained Make target", result.stderr)
                            self.assertNotIn("Traceback", result.stderr)
                            self.assertEqual("", result.stdout)
                            self.assertEqual([], list(Path(directory).iterdir()))

    def test_exact_marker_permits_fresh_import_without_running_commands(self):
        for module in MODULES:
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-B", "-c", f"import tools.{module}"],
                    env={**os.environ, "NMRNET_IN_CONTAINER": "1"},
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, msg=result.stderr)
                self.assertEqual("", result.stdout)
                self.assertEqual("", result.stderr)


class ToolingLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "tools").mkdir()
        for name in ("container", "docker", "docker_client", "inference_native", "packages", "wheelhouse"):
            (self.root / f"tools/{name}.sh").write_bytes(Path(f"tools/{name}.sh").read_bytes())
        (self.root / "requirements").mkdir()
        for name in ("inference.in", "build.in", "unicore-source.json", "inference.lock"):
            (self.root / f"requirements/{name}").write_text("fixture")
        (self.root / ".work/wheelhouse").mkdir(parents=True)
        (self.root / ".work/packages-stage/lock.TEST").mkdir(parents=True)
        (self.root / ".work/packages-stage/lock.TEST/image-id").write_text("sha256:" + "a" * 64 + "\n")

    def commands(self, owner, lane):
        # An incorrect ambient marker must not alter the fixed container grant.
        result = subprocess.run(
            ["bash", str(self.root / f"tools/{owner}.sh"), "--print", lane],
            env={
                **os.environ,
                "NMRNET_IN_CONTAINER": "0",
                "NMRNET_WLAN_INTERFACE_INPUT": "lo",
                "NMRNET_STAGE_INPUT": "lock.TEST",
            },
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, msg=result.stderr)
        return [shlex.split(line) for line in result.stdout.splitlines()]

    def test_ambient_stage_does_not_authorize_apply(self):
        environment = {**os.environ, "STAGE": "lock.TEST"}
        environment.pop("NMRNET_STAGE_INPUT", None)
        result = subprocess.run(
            ["bash", str(self.root / "tools/packages.sh"), "--print", "apply"],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertIn("make packages/lock/apply", result.stderr)

    def test_invalid_stage_alias_rejects_before_command_or_state_write(self):
        for stage in ("../lock.TEST", "lock.TEST/../", "lock.$(private-input)"):
            with self.subTest(stage=stage):
                result = subprocess.run(
                    ["bash", str(self.root / "tools/packages.sh"), "--print", "apply"],
                    env={**os.environ, "NMRNET_STAGE_INPUT": stage},
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertNotIn("private-input", result.stderr)
                self.assertEqual(["lock.TEST"], sorted(path.name for path in (self.root / ".work/packages-stage").iterdir()))

    def assert_guarded_module(self, command, module, writable):
        self.assertIn("NMRNET_IN_CONTAINER=1", command)
        self.assertNotIn("NMRNET_IN_CONTAINER=0", command)
        self.assertEqual("/workspace", command[command.index("--workdir") + 1])
        self.assertEqual(f"tools.{module}", command[command.index("-m") + 1])
        mounts = [command[index + 1] for index, item in enumerate(command) if item == "--mount"]
        self.assertEqual(
            [f"type=bind,src={self.root}/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"],
            [mount for mount in mounts if "container_guard.py" in mount],
        )
        self.assertIn(f"type=bind,src={self.root}/tools/{module}.py,dst=/workspace/tools/{module}.py,readonly", mounts)
        self.assertEqual(writable, [mount for mount in mounts if not mount.endswith(",readonly")])

    def test_probe_reads_guard_and_source_and_only_writes_ownership_output(self):
        self.assert_guarded_module(
            self.commands("container", "probe")[0], "probe", [f"type=bind,src={self.root}/.work/probe.PRINT,dst=/output"]
        )

    def test_package_apply_reads_guard_stage_and_only_writes_requirements(self):
        self.assert_guarded_module(
            self.commands("packages", "apply")[0], "package_lock", [f"type=bind,src={self.root}/requirements,dst=/output"]
        )

    def test_package_stage_supplies_fixed_marker_to_image_entrypoint(self):
        commands = self.commands("packages", "stage")
        worker = next(command for command in commands if "sha256:LOCK_IMAGE_ID" in command)
        self.assertIn("NMRNET_IN_CONTAINER=1", worker)
        self.assertNotIn("NMRNET_IN_CONTAINER=0", worker)

    def test_wheelhouse_lanes_read_guard_and_keep_their_write_boundary(self):
        for lane in ("check", "download"):
            with self.subTest(lane=lane):
                command = next(command for command in self.commands("wheelhouse", lane) if "tools.wheelhouse" in command)
                writable = [] if lane == "check" else [f"type=bind,src={self.root}/.work/wheelhouse,dst=/wheelhouse"]
                self.assert_guarded_module(command, "wheelhouse", writable)


if __name__ == "__main__":
    unittest.main()
