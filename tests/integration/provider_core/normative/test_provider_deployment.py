"""Deployment freezing preserves actual inputs without observation-file handoffs."""

import hashlib
import io
import json
import os
import socket
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tests.integration.nmrnet.compose_ports import deployment_inputs
from tools.provider_deployment import client_inputs, freeze_deployment, main, prepare_runtime, require_old_api_route
from nmrnet_provider.config import ConfigurationRejected
from tests.integration.nmrnet.compose_ports import ComposeFixture


class DeploymentBundleTests(unittest.TestCase):
    def test_runtime_preparation_admits_only_owned_private_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            for name in ("prediction", "reader", "smiles"):
                leaf = runtime / name
                leaf.mkdir(mode=0o700)
                (leaf / ".owner-session.lock").write_text("")
                (leaf / ".owner-session.lock").chmod(0o600)
            for name in ("route-interpreter", "route-store"):
                leaf = runtime / name
                leaf.mkdir(mode=0o700)
                ready = leaf / "ready"
                ready.write_text("")
                ready.chmod(0o600)
                listener = socket.socket(socket.AF_UNIX)
                try:
                    listener.bind(str(leaf / "proxy.sock"))
                finally:
                    listener.close()
                (leaf / "proxy.sock").chmod(0o600)
            prepare_runtime(runtime, os.getuid(), os.getgid())
            for leaf in runtime.iterdir():
                self.assertEqual((leaf.stat().st_uid, stat.S_IMODE(leaf.stat().st_mode)), (os.getuid(), 0o700))
                for entry in leaf.iterdir():
                    self.assertEqual((entry.stat().st_uid, stat.S_IMODE(entry.stat().st_mode)), (os.getuid(), 0o600))

    def test_runtime_preparation_rejects_unknown_entries_before_ownership_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            runtime.mkdir(mode=0o700)
            for name in ("prediction", "reader", "smiles", "route-interpreter", "route-store"):
                (runtime / name).mkdir(mode=0o700)
            unexpected = runtime / "route-store" / "other"
            unexpected.write_text("preserve")
            with patch("tools.provider_deployment.os.chown") as chown:
                with self.assertRaisesRegex(ConfigurationRejected, "unowned entry"):
                    prepare_runtime(runtime, os.getuid(), os.getgid())
            chown.assert_not_called()
            self.assertEqual(unexpected.read_text(), "preserve")

    def test_runtime_preparation_rejects_external_hardlinks_before_ownership_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700)
            for name in ("prediction", "reader", "smiles", "route-interpreter", "route-store"):
                (runtime / name).mkdir(mode=0o700)
            external = root / "external-lock"
            external.write_text("retained")
            external.chmod(0o600)
            original = external.stat()
            (runtime / "prediction/.owner-session.lock").hardlink_to(external)
            with patch("tools.provider_deployment.os.chown") as chown:
                with self.assertRaisesRegex(ConfigurationRejected, "another hard link"):
                    prepare_runtime(runtime, os.getuid(), os.getgid())
            chown.assert_not_called()
            self.assertEqual((external.stat().st_uid, external.stat().st_mode & 0o777), (original.st_uid, 0o600))
            self.assertEqual(external.read_text(), "retained")

    def test_old_api_route_admission_uses_the_retained_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments, _ = deployment_inputs(Path(directory))
            bundle = arguments["bundle"]
            route = bundle.parent.parent / "runtime/route-api"
            (bundle / "compose.json").write_text(json.dumps({"services": {"api": {"volumes": [
                {"type": "bind", "source": str(route), "target": "/route", "read_only": False}
            ]}}}))
            (bundle / "compose.json").chmod(0o600)
            require_old_api_route(bundle, route)
            with self.assertRaises(ConfigurationRejected):
                require_old_api_route(bundle, route.parent / "route-store")

    def test_freeze_cli_reports_unsupported_retry_without_starting_services(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments, _ = deployment_inputs(Path(directory))
            definition = arguments["definition"]
            definition.write_text(definition.read_text().replace("max_retries = 0", "max_retries = 2"))
            argv = ["freeze"]
            for key, value in arguments.items():
                if key == "source_names":
                    for source in value:
                        argv.extend(("--source", str(source)))
                else:
                    argv.extend(("--" + key.replace("_", "-"), str(value)))
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(main(argv), 2)
            self.assertEqual(output.getvalue(), "")
            self.assertIn("scientific_retry.max_retries", errors.getvalue())
            self.assertNotIn("Traceback", errors.getvalue())
            self.assertFalse((arguments["bundle"] / "compose.json").exists())
            self.assertFalse((arguments["bundle"] / "config/provider.toml").exists())
            self.assertEqual([p for p in (arguments["bundle"] / "secrets").rglob("*") if p.is_file()], [])

    def test_client_expectations_stream_current_image_without_exporting_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments, files = deployment_inputs(Path(directory))
            freeze_deployment(**arguments)
            output = io.StringIO()
            with redirect_stdout(output):
                client_inputs(bundle=arguments["bundle"], prediction_image_id=arguments["image_id"],
                              client_root=Path(directory) / "client")
            raw = output.getvalue()
            identity = json.loads(raw)
            self.assertEqual(identity["prediction_image_id"], arguments["image_id"])
            self.assertEqual(identity["bundle"], arguments["bundle"].name)
            self.assertEqual(identity["provider_ref"], "provider:example")
            plan = json.loads((arguments["bundle"] / "compose.json").read_bytes())
            checkpoint_mount = next(mount for mount in plan["services"]["provider"]["volumes"]
                                    if mount["type"] == "volume" and not mount["read_only"])
            self.assertEqual(checkpoint_mount["target"], "/srv/nmrnet/attempts")
            self.assertNotIn(files.key.read_text().strip(), raw)
            self.assertNotIn("key_file", raw)
            self.assertNotIn("credential", raw)
            rejected = io.StringIO()
            with redirect_stdout(rejected):
                with self.assertRaisesRegex(ValueError, "prediction image differs"):
                    client_inputs(bundle=arguments["bundle"], prediction_image_id="sha256:" + "9" * 64,
                                  client_root=Path(directory) / "client")
                with self.assertRaisesRegex(ValueError, "overlap"):
                    client_inputs(bundle=arguments["bundle"], prediction_image_id=arguments["image_id"],
                                  client_root=arguments["bundle"] / "client")
            self.assertEqual(rejected.getvalue(), "")

    def test_refresh_preserves_old_generation_and_exact_actual_source_and_secret_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments, files = deployment_inputs(Path(directory))
            first = arguments["bundle"]
            freeze_deployment(**arguments)
            previous = {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()}
            arguments["definition"].write_text(arguments["definition"].read_text().replace("max_turns = 6", "max_turns = 5"))
            second = first.parent / "generation-second"
            second.mkdir(mode=0o700)
            (second / "config").mkdir(mode=0o700)
            (second / "secrets").mkdir(mode=0o700)
            (second / "secrets/provider").mkdir(mode=0o700)
            arguments["bundle"] = second
            freeze_deployment(**arguments)
            self.assertEqual(previous, {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()})
            self.assertNotEqual((first / "config/provider.toml").read_bytes(), (second / "config/provider.toml").read_bytes())
            self.assertEqual((second / "secrets/provider/signing.private.json").read_bytes(), files.credential.read_bytes())
            self.assertEqual((second / "secrets/provider/interpreter.key").read_bytes(), files.key.read_bytes())
            plan = json.loads((second / "compose.json").read_bytes())
            self.assertEqual(len(plan["services"]), 6)
            self.assertNotIn("api", plan["services"])
            provider = plan["services"]["provider"]
            self.assertEqual(provider["group_add"], ["1001"])
            self.assertIn("fixture-delivery-outbox-socket", plan["volumes"])
            socket = provider["command"][provider["command"].index("--api-proxy-socket") + 1]
            grant = next(item for item in provider["volumes"] if socket.startswith(item["target"] + "/"))
            self.assertEqual(grant["source"], "fixture-delivery-proxy-socket")
            self.assertTrue(grant["read_only"])
            self.assertEqual(plan["name"], json.loads((first / "compose.json").read_bytes())["name"])
            self.assertEqual((second / "resolv.conf").read_text(), "nameserver 192.0.2.53\n")
            self.assertEqual((second / "execution.json").stat().st_mode & 0o777, 0o444)
            self.assertEqual((second / "compose.json").stat().st_mode & 0o777, 0o444)
            self.assertEqual((second / "secrets/provider/signing.private.json").stat().st_mode & 0o777, 0o400)
            self.assertEqual((second / "config").stat().st_mode & 0o777, 0o555)
            execution = json.loads((second / "execution.json").read_bytes())
            self.assertEqual(execution["runtime_identity_source"], "caller")
            self.assertEqual(execution["image_id"], arguments["image_id"])
            self.assertEqual(execution["repository_revision"], arguments["repository_revision"])
            expected = {}
            for name in arguments["source_names"]:
                raw = (arguments["source_root"] / name).read_bytes()
                expected[name] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
            self.assertEqual(execution["tool_sources"], expected)
            for name in ("inference.lock", "unicore-source.json"):
                self.assertEqual((second / name).read_bytes(), (arguments["source_root"] / "requirements" / name).read_bytes())
            self.assertEqual((second / "prediction-models.json").read_bytes(), arguments["models"].read_bytes())
            self.assertNotIn("PRIVATE_MODEL_KEY", (second / "compose.json").read_text())
            self.assertEqual({path.relative_to(second).as_posix() for path in second.rglob("*") if path.is_file()}, {
                "config/provider.toml", "secrets/provider/signing.private.json", "secrets/provider/interpreter.key",
                "execution.json", "prediction-models.json", "inference.lock", "unicore-source.json",
                "compose.json", "resolv.conf",
            })


class ProviderNamespacePlanTests(ComposeFixture, unittest.TestCase):
    def test_production_default_plan_is_unchanged_and_private_checkout_changes_all_provider_names(self):
        default = self.plan()
        private = self.plan(checkout=self.root / "private-checkout")
        self.assertNotEqual(default["name"], private["name"])
        self.assertEqual(default["volumes"], private["volumes"])
        self.assertEqual(set(default["services"]), set(private["services"]))
        self.assertTrue({service["container_name"] for service in default["services"].values()}.isdisjoint(
            service["container_name"] for service in private["services"].values()))
        for role in private["services"]:
            self.assertEqual(default["services"][role]["command"], private["services"][role]["command"])
