"""Consumer lanes grant released artifacts and scientific bindings, without SDK source."""

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.provider_consumer_tests import discover_suite


class ConsumerLauncherTests(unittest.TestCase):
    def test_discovery_uses_default_or_explicit_patterns_and_refuses_empty_selection(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules), patch.object(sys, "path", list(sys.path)):
            root = Path(directory)
            package = root / "consumer_discovery_fixture"
            package.mkdir()
            (package / "__init__.py").write_text("")
            for name in ("test_default.py", "chosen_extra.py"):
                (package / name).write_text("import unittest\nclass Case(unittest.TestCase):\n    def test_case(self): pass\n")
            for patterns in (None, ["chosen*.py"]):
                with self.subTest(patterns=patterns):
                    result = unittest.TestResult()
                    discover_suite(package, root, patterns).run(result)
                    self.assertTrue(result.wasSuccessful())
                    self.assertEqual(1, result.testsRun)
            with self.assertRaisesRegex(RuntimeError, "contains no tests"):
                discover_suite(package, root, ["missing*.py"])
            (package / "test_default.py").unlink()
            with self.assertRaisesRegex(RuntimeError, "Default NMRNet consumer discovery contains no tests"):
                discover_suite(package, root, None)

    def test_focused_lanes_are_offline_and_do_not_select_displaced_generic_tests(self):
        selections = {
            "config": ["test_nmrnet_config.py"],
            "uploads": ["test_nmrnet_upload_session.py", "test_upload_preparation.py", "test_nmrnet_upload_preparation.py"],
            "prepared-identity": ["test_nmrnet_session.py", "test_nmrnet_prediction_recovery.py"],
            "delivery": ["test_nmrnet_service_recovery.py"],
        }
        with tempfile.TemporaryDirectory() as pool:
            host_bin = Path(pool) / "host-bin"
            host_bin.mkdir()
            (host_bin / "id").symlink_to(Path("/workspace/repository_checks/fixtures/containers/id.sh"))
            for name, patterns in selections.items():
                with self.subTest(selection=name):
                    result = subprocess.run(
                        ["bash", "tools/container.sh", "--print", "test/integration/provider-core/" + name],
                        env={
                            **os.environ,
                            "PATH": str(host_bin) + ":" + os.environ["PATH"],
                            "NMRNET_FIXTURE_UID": "1000",
                            "NMRNET_FIXTURE_GID": "1000",
                            "NMRNET_DOCKER_CONTEXT_INPUT": "test-local",
                            "NMRNET_DOCKER_CONTEXT_INPUT_ORIGIN": "command line",
                            "NMRNET_DOCKER_ENDPOINT_INPUT": "unix:///var/run/docker.sock",
                            "NMRNET_WHEELHOUSE_INPUT": pool,
                            "NMRNET_WHEELHOUSE_INPUT_ORIGIN": "command line",
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(0, result.returncode, msg=result.stderr)
                    command = shlex.split(result.stdout.splitlines()[-1])
                    self.assertEqual("none", command[command.index("--network") + 1])
                    self.assertEqual(command[command.index("tools.provider_test_environment") + 1 :], ["integration", *patterns])
                    mounts = [command[index + 1] for index, word in enumerate(command) if word == "--mount"]
                    self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
                    self.assertTrue(any(mount.endswith(",dst=/input/provider-core-install.lock,readonly") for mount in mounts))
                    self.assertTrue(any(mount.endswith(",dst=/input/provider-core-release.json,readonly") for mount in mounts))
                    self.assertFalse(
                        any("packages/provider-core" in mount or "dst=/workspace/provider_core" in mount for mount in mounts)
                    )
