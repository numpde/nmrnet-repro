"""A frozen generation contains the final Compose plan and exact admitted inputs."""

import json
import tempfile
import unittest
from pathlib import Path

from tests.integration.nmrnet.compose_ports import deployment_inputs
from tools.provider_deployment import client_inputs, finalize_deployment, freeze_deployment


class DeploymentBundleTests(unittest.TestCase):
    def test_client_expectations_use_original_bundle_and_current_image_without_exporting_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments, files = deployment_inputs(Path(directory))
            freeze_deployment(**arguments)
            output = Path(directory) / "client.json"
            client_inputs(bundle=arguments["bundle"], prediction_image_id=arguments["image_id"],
                          client_root=Path(directory) / "client", output=output)
            raw = output.read_text()
            identity = json.loads(raw)
            self.assertEqual(identity["prediction_image_id"], arguments["image_id"])
            self.assertEqual(identity["bundle"], arguments["bundle"].name)
            self.assertEqual(identity["provider_ref"], "provider:example")
            self.assertNotIn(files.key.read_text().strip(), raw)
            self.assertNotIn("key_file", raw)
            self.assertNotIn("credential", raw)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            rejected = Path(directory) / "rejected.json"
            with self.assertRaisesRegex(ValueError, "prediction image differs"):
                client_inputs(bundle=arguments["bundle"], prediction_image_id="sha256:" + "9" * 64,
                              client_root=Path(directory) / "client", output=rejected)
            self.assertFalse(rejected.exists())
            with self.assertRaisesRegex(ValueError, "overlap"):
                client_inputs(bundle=arguments["bundle"], prediction_image_id=arguments["image_id"],
                              client_root=Path(directory) / "attempts/client", output=rejected)
            self.assertFalse(rejected.exists())

    def test_refresh_preserves_old_generation_and_publishes_final_compose_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments, files = deployment_inputs(Path(directory))
            output = Path(directory) / "output"
            first = arguments["bundle"]
            freeze_deployment(**arguments)
            self.assertFalse((first / "complete").exists())
            self.assertFalse((output / "bundle-name").exists())
            # The contained contract lane supplies normalization stand-ins.
            # The production host lane must retain the actual CLI result.
            normalized = Path(directory) / "normalized.json"
            normalized.write_bytes((first / "compose.json").read_bytes())
            finalize_deployment(first, normalized, output)
            previous = {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()}
            arguments["definition"].write_text(arguments["definition"].read_text().replace("max_turns = 6", "max_turns = 5"))
            second = first.parent / "generation-second"
            second.mkdir(mode=0o700)
            arguments["bundle"] = second
            freeze_deployment(**arguments)
            normalized.write_bytes((second / "compose.json").read_bytes())
            finalize_deployment(second, normalized, output)
            self.assertEqual(previous, {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()})
            self.assertNotEqual((first / "config/provider.toml").read_bytes(), (second / "config/provider.toml").read_bytes())
            self.assertEqual((second / "secrets/signing.private.json").read_bytes(), files.credential.read_bytes())
            self.assertEqual((second / "secrets/interpreter.key").read_bytes(), files.key.read_bytes())
            plan = json.loads((second / "compose.json").read_bytes())
            self.assertEqual(len(plan["services"]), 7)
            self.assertEqual(plan["name"], json.loads((first / "compose.json").read_bytes())["name"])
            self.assertEqual((output / "bundle-name").read_text(), "generation-second\n")
            self.assertEqual((second / "complete").read_bytes(), b"complete\n")
            self.assertEqual((second / "resolv.conf").read_text(), "nameserver 192.0.2.53\n")
            self.assertEqual((second / "execution.json").stat().st_mode & 0o777, 0o400)
            self.assertEqual(second.stat().st_mode & 0o777, 0o500)
            for path in second.rglob("*"):
                self.assertEqual(path.stat().st_mode & 0o777, 0o500 if path.is_dir() else 0o400)
            self.assertNotIn("PRIVATE_MODEL_KEY", (second / "compose.json").read_text())
            self.assertFalse((output / "service.env").exists())
            self.assertNotIn("UNIT", (second / "SOURCE.txt").read_text())
