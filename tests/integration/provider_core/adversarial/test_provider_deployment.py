"""Refused deployment inputs cannot publish a complete selected generation."""

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.integration.nmrnet.compose_ports import deployment_inputs
from tools.provider_deployment import main


class DeploymentRefusalTests(unittest.TestCase):
    def test_unsafe_secret_and_invalid_route_or_dns_never_publish_completion(self):
        for failure in ("permissions", "escape", "missing", "interface", "resolvers", "legacy-profile"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                arguments, files = deployment_inputs(Path(directory))
                if failure == "permissions":
                    files.key.chmod(0o644)
                elif failure == "escape":
                    definition = arguments["definition"]
                    definition.write_bytes(definition.read_bytes().replace(b"/srv/nmrnet/input/interpreter.key", b"/srv/nmrnet/input/../outside.key"))
                elif failure == "missing":
                    files.key.unlink()
                else:
                    settings = arguments["settings"]
                    raw = settings.read_text()
                    if failure == "interface":
                        raw = raw.replace('interface = "eth0"', 'interface = ""')
                    elif failure == "resolvers":
                        raw = raw.replace("192.0.2.53", "127.0.0.53")
                    else:
                        raw = 'service_env = "legacy.env"\n' + raw
                    settings.write_text(raw)
                command = [word for name, value in arguments.items() for word in ("--" + name.replace("_", "-"), str(value))]
                errors = io.StringIO()
                with patch("sys.stderr", errors):
                    self.assertEqual(main(command), 2)
                self.assertNotIn("PRIVATE_MODEL_KEY", errors.getvalue())
                self.assertFalse((arguments["bundle"] / "complete").exists())
                self.assertFalse((arguments["output"] / "bundle-name").exists())
                if failure == "missing":
                    self.assertIn("FileNotFoundError", errors.getvalue())
                    self.assertIn('"errno": 2', errors.getvalue())
