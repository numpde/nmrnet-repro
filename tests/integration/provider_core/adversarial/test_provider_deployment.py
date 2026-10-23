"""Only declared protected inputs can become a ready deployment bundle."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.provider_deployment import main

from ..config_ports import ConfigFiles


class DeploymentRefusalTests(unittest.TestCase):
    def test_unsafe_key_or_escaping_reference_never_publishes_service_profile(self):
        for failure in ("permissions", "escape", "missing"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                secrets = root / "secrets"
                secrets.mkdir(mode=0o700)
                files = ConfigFiles(secrets)
                definition = root / "provider.toml"
                raw = files.raw.replace(str(files.state).encode(), b"/srv/nmrnet/attempts")
                raw = raw.replace(str(secrets).encode(), b"/srv/nmrnet/input")
                if failure == "escape":
                    raw = raw.replace(b"/srv/nmrnet/input/interpreter.key", b"/srv/nmrnet/input/../outside.key")
                elif failure == "permissions":
                    files.key.chmod(0o644)
                else:
                    files.key.unlink()
                definition.write_bytes(raw)
                settings = root / "deployment.toml"
                settings.write_text("store_root = " + json.dumps(str(root / "attempts")) + "\nmaximum_startup_bytes = 65536\n")
                profile = root / "service.env"
                profile.write_text('NMRNET_API_ROUTE_INPUT="external"\n')
                profile.chmod(0o600)
                bundle, output = root / "bundle", root / "output"
                bundle.mkdir(mode=0o700)
                output.mkdir(mode=0o700)
                arguments = []
                for name, value in (
                    ("definition", definition),
                    ("settings", settings),
                    ("profile", profile),
                    ("secrets", secrets),
                    ("bundle", bundle),
                    ("output", output),
                ):
                    arguments.extend(("--" + name, str(value)))
                errors = io.StringIO()
                with patch("sys.stderr", errors):
                    status = main(arguments)
                self.assertEqual(status, 2)
                json.loads(errors.getvalue())
                self.assertFalse((output / "service.env").exists())
                self.assertNotIn("PRIVATE_MODEL_KEY", errors.getvalue())
                if failure == "missing":
                    self.assertIn("FileNotFoundError", errors.getvalue())
                    self.assertIn('"errno": 2', errors.getvalue())
