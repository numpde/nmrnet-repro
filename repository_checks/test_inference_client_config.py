"""Observe inference's owned client config independently of caller defaults."""

from tools.container_guard import require_container

require_container()

import json
import os
from pathlib import Path
import signal
import shlex
import subprocess
import tempfile
import time
import unittest

from repository_checks.fixtures.containers.fixture import LauncherFixture


class InferenceBuildInputAdmissionTests(unittest.TestCase):
    def test_unsafe_requirement_leaf_never_reaches_engine(self):
        for name in ("inference.lock", "unicore-source.json"):
            for kind in ("symlink", "missing", "directory", "fifo"):
                with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                    fixture = LauncherFixture(Path(directory))
                    candidate = fixture.root / "requirements" / name
                    candidate.unlink()
                    if kind == "symlink":
                        outside = fixture.root / "outside-requirement"
                        outside.write_text("outside-file-must-not-be-mounted\n")
                        candidate.symlink_to(outside)
                    elif kind == "directory":
                        candidate.mkdir()
                    elif kind == "fifo":
                        os.mkfifo(candidate)
                    result = fixture.run("inference_image.sh", "build")
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(name, result.stderr)
                    self.assertEqual(fixture.calls(), [], "Unsafe requirements reached an engine dispatch")

    def test_unsafe_requirements_parent_never_reaches_engine(self):
        for kind in ("symlink", "missing", "file", "fifo"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                requirements = fixture.root / "requirements"
                saved = fixture.root / "outside-requirements"
                requirements.rename(saved)
                if kind == "symlink":
                    requirements.symlink_to(saved, target_is_directory=True)
                elif kind == "file":
                    requirements.write_text("not-a-directory\n")
                elif kind == "fifo":
                    os.mkfifo(requirements)
                result = fixture.run("inference_image.sh", "build")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("requirements", result.stderr)
                self.assertEqual(fixture.calls(), [], "An unsafe requirements ancestor reached the engine")


class InferenceClientConfigTests(unittest.TestCase):
    def caller_configuration(self, root):
        home = root / "caller"
        configuration = home / ".docker"
        configuration.mkdir(parents=True)
        marker = "synthetic-caller-auth-and-proxy"
        body = json.dumps({"auths": {"registry.invalid": {"auth": marker}},
                           "proxies": {"default": {"httpProxy": marker}}})
        (configuration / "config.json").write_text(body)
        override = root / "caller-override"
        override.mkdir()
        (override / "config.json").write_text(body)
        return {"HOME": str(home), "DOCKER_CONFIG": str(override)}, (configuration, override)

    def assert_owned_configuration(self, fixture, caller_paths, *, retired=True):
        calls = fixture.calls()
        self.assertTrue(calls, "The actual launcher never reached the engine double")
        configurations = [call["client_config"] for call in calls]
        self.assertTrue(all(configuration is not None for configuration in configurations))
        paths = {configuration["path"] for configuration in configurations}
        self.assertEqual(len(paths), 1, "Preflight and execution did not share one owned client authority")
        path = Path(paths.pop())
        self.assertNotIn(path, caller_paths)
        self.assertEqual(path.parent, Path("/tmp"))
        self.assertTrue(path.name.startswith("nmrnet-inference-client."))
        for configuration in configurations:
            self.assertEqual(configuration["kind"], "directory")
            self.assertEqual(configuration["uid"], os.getuid())
            self.assertEqual(configuration["mode"], 0o700)
            self.assertEqual(configuration["entries"], [])
        self.assertEqual(path.exists(), not retired)
        for caller in caller_paths:
            self.assertTrue((caller / "config.json").is_file())
        return path

    def test_print_selects_inert_configuration_without_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            environment, _ = self.caller_configuration(fixture.root)
            result = fixture.run("inference_image.sh", "--print", "import", additions=environment)
            self.assertEqual(result.returncode, 0, result.stderr)
            arguments = shlex.split(result.stdout)
            self.assertEqual(arguments[arguments.index("--config") + 1], "/tmp/nmrnet-inference-client.PRINT")
            self.assertEqual(fixture.calls(), [])

    def test_unpublished_image_record_is_removed_after_publication_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            environment, caller_paths = self.caller_configuration(fixture.root)
            record = fixture.root / ".work/inference/image-id"
            before = record.read_bytes(), record.stat().st_ino
            failing_publication = ('() { case "$*" in */image-id.*) return 11;; '
                                   '*) command mv "$@";; esac; }')
            result = fixture.run("inference_image.sh", "build",
                                 additions=environment | {"BASH_FUNC_mv%%": failing_publication})
            self.assertEqual(result.returncode, 11, result.stderr)
            self.assertEqual((record.read_bytes(), record.stat().st_ino), before)
            self.assertEqual({path.name for path in record.parent.iterdir()}, {"image-id"})
            self.assert_owned_configuration(fixture, caller_paths)

    def test_build_and_import_select_empty_owned_configuration_then_retire_it(self):
        for action in ("build", "import"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                environment, caller_paths = self.caller_configuration(fixture.root)
                result = fixture.run("inference_image.sh", action, additions=environment)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_owned_configuration(fixture, caller_paths)
                if action == "build":
                    commands = [call["arguments"][0] for call in fixture.calls()]
                    self.assertIn("build", commands)
                    self.assertIn("run", commands)

    def test_failed_build_and_expired_import_retire_owned_configuration(self):
        cases = [("build", {"NMRNET_FAKE_BUILD_STATUS": "7"}, 7),
                 ("import", {"NMRNET_FAKE_HOLD_MODE": "eof"}, 124)]
        for action, additions, expected in cases:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                environment, caller_paths = self.caller_configuration(fixture.root)
                arguments = ("--timeout", "0.1", action) if action == "import" else (action,)
                result = fixture.run("inference_image.sh", *arguments, additions=environment | additions)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assert_owned_configuration(fixture, caller_paths)

    def test_cancellation_retires_configuration_after_joining_native_client(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            environment, caller_paths = self.caller_configuration(fixture.root)
            process = subprocess.Popen(["bash", str(fixture.root / "tools/inference_image.sh"), "import"],
                                       env=fixture.environment | environment | {"NMRNET_FAKE_HOLD_MODE": "eof"},
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       start_new_session=True)
            try:
                deadline = time.monotonic() + 2
                while len(fixture.calls()) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(len(fixture.calls()), 2, "The import client did not dispatch before cancellation")
                process.send_signal(signal.SIGTERM)
                _, stderr = process.communicate(timeout=2)
                self.assertEqual(process.returncode, 143, stderr)
                self.assert_owned_configuration(fixture, caller_paths)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=2)

    def test_cleanup_failure_keeps_primary_status_and_retained_configuration(self):
        failing_removal = ('() { case "$*" in *nmrnet-inference-client.*) return 9;; '
                           '*) command rm "$@";; esac; }')
        for primary, expected in ((0, 9), (7, 7)):
            with self.subTest(primary=primary), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                environment, caller_paths = self.caller_configuration(fixture.root)
                additions = environment | {"BASH_FUNC_rm%%": failing_removal}
                if primary:
                    additions["NMRNET_FAKE_BUILD_STATUS"] = str(primary)
                result = fixture.run("inference_image.sh", "build", additions=additions)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertIn("owned Docker client configuration", result.stderr)
                retained = self.assert_owned_configuration(fixture, caller_paths, retired=False)
                retained.rmdir()
