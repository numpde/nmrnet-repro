"""Deployment refresh freezes edited definitions without changing existing bundles."""

import json
import tempfile
import unittest
from pathlib import Path

from nmrnet_provider.startup_capture import capture_startup
from tools.provider_deployment import freeze_deployment

from ..config_ports import ConfigFiles


class DeploymentBundleTests(unittest.TestCase):
    def test_refresh_admits_issued_format_and_preserves_previous_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secrets = root / "secrets"
            secrets.mkdir(mode=0o700)
            files = ConfigFiles(secrets)
            definition = root / "provider.toml"
            definition.write_bytes(
                files.raw.replace(str(files.state).encode(), b"/srv/nmrnet/attempts").replace(
                    str(secrets).encode(), b"/srv/nmrnet/input"
                )
            )
            settings = root / "deployment.toml"
            settings.write_text("store_root = " + json.dumps(str(root / "attempts")) + "\nmaximum_startup_bytes = 65536\n")
            profile = root / "service.env"
            profile.write_text('NMRNET_API_ROUTE_INPUT="external"\n')
            profile.chmod(0o600)
            first = root / "generation-first"
            second = root / "generation-second"
            for bundle in (first, second):
                bundle.mkdir(mode=0o700)
            output = root / "output"
            output.mkdir(mode=0o700)
            for bundle in (first, second):
                freeze_deployment(definition, settings, profile, secrets, bundle, output)
                if bundle == first:
                    previous = (first / "config/provider.toml").read_bytes()
                    definition.write_text(definition.read_text().replace("max_turns = 6", "max_turns = 5"))
            self.assertEqual((first / "config/provider.toml").read_bytes(), previous)
            self.assertNotEqual((second / "config/provider.toml").read_bytes(), previous)
            self.assertEqual((second / "secrets/signing.private.json").read_bytes(), files.credential.read_bytes())
            self.assertEqual((second / "secrets/interpreter.key").read_bytes(), files.key.read_bytes())
            plan = capture_startup(
                second / "config/provider.toml", source_root=second, destination=root / "snapshot", maximum_bytes=65536
            )
            self.assertEqual(plan["store_root"], str(root / "attempts"))
            installed = (output / "service.env").read_text()
            self.assertIn(str(second / "config/provider.toml"), installed)
            self.assertIn('NMRNET_API_ROUTE_INPUT="external"', installed)
            self.assertNotIn("PRIVATE_MODEL_KEY", installed)
            self.assertEqual((second / "secrets/interpreter.key").stat().st_mode & 0o777, 0o400)
