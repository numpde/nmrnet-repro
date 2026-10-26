"""Consumer lanes grant released artifacts and scientific bindings, without SDK source."""

import os
import shlex
import subprocess
import tempfile
import unittest


class ConsumerLauncherTests(unittest.TestCase):
    def test_focused_lanes_are_offline_and_do_not_select_displaced_generic_tests(self):
        selections = {
            "config": ["test_nmrnet_config.py"],
            "uploads": ["test_nmrnet_upload_session.py", "test_upload_preparation.py", "test_nmrnet_upload_preparation.py"],
            "prepared-identity": ["test_nmrnet_session.py", "test_nmrnet_prediction_recovery.py"],
            "delivery": ["test_nmrnet_service_delivery.py"],
        }
        with tempfile.TemporaryDirectory() as pool:
            for name, patterns in selections.items():
                with self.subTest(selection=name):
                    result = subprocess.run(
                        ["bash", "tools/container.sh", "--print", "test/integration/provider-core/" + name],
                        env={**os.environ, "NMRNET_WHEELHOUSE_INPUT": pool,
                             "NMRNET_WHEELHOUSE_INPUT_ORIGIN": "command line"},
                        capture_output=True, text=True, check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    command = shlex.split(result.stdout.splitlines()[-1])
                    self.assertEqual(command[command.index("--network") + 1], "none")
                    self.assertEqual(command[command.index("tools.provider_test_environment") + 1:], ["integration", *patterns])
                    mounts = [command[index + 1] for index, word in enumerate(command) if word == "--mount"]
                    self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
                    self.assertTrue(any(mount.endswith(",dst=/input/provider-core-install.lock,readonly") for mount in mounts))
                    self.assertTrue(any(mount.endswith(",dst=/input/provider-core-release.json,readonly") for mount in mounts))
                    self.assertFalse(any("packages/provider-core" in mount or "dst=/workspace/provider_core" in mount for mount in mounts))
