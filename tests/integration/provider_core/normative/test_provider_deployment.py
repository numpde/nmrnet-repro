"""Deployment freezing preserves actual inputs without observation-file handoffs."""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.compose_ports import deployment_inputs
from tools.provider_deployment import client_inputs, freeze_deployment
from nmrnet_provider.delivery_deployment import DeliveryDeploymentRejected, prepare
from tests.integration.nmrnet.compose_ports import ComposeFixture


class DeploymentBundleTests(unittest.TestCase):
    def test_private_delivery_recipe_isolates_all_engine_names_without_changing_wire_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments, _ = deployment_inputs(root)
            uid, gid = os.geteuid(), os.getegid()
            namespace = "nmr-qualification-private"
            arguments.update(uid=uid, gid=gid, delivery_project=namespace)
            freeze_deployment(**arguments)
            bundle = arguments["bundle"]
            provider = json.loads((bundle / "compose.json").read_bytes())
            bundle.chmod(0o500)
            output = root / "delivery"

            def recipe(project):
                output.mkdir(mode=0o700)
                keywords = {} if project is None else {"project": project}
                prepare([bundle], output, uid, gid, arguments["controller_image_id"],
                        arguments["controller_image_id"], **keywords)
                result = ((output / "compose.json").read_bytes(), (output / "daemon.toml").read_bytes())
                shutil.rmtree(output)
                return result

            default = recipe(None)
            self.assertEqual(default, recipe("nmr-api-delivery"))
            private_raw, private_daemon = recipe(namespace)
            original, private = json.loads(default[0]), json.loads(private_raw)
            self.assertEqual(private["name"], namespace)
            self.assertNotEqual(provider["name"], private["name"])
            self.assertEqual(default[1], private_daemon)
            self.assertEqual(set(private["services"]), set(original["services"]))
            self.assertEqual(len(private["volumes"]), 3)
            self.assertTrue(set(private["volumes"]).isdisjoint(original["volumes"]))
            self.assertTrue(all(name.startswith(namespace + "-") for name in private["volumes"]))
            self.assertTrue(set(provider["volumes"]) <= set(private["volumes"]))
            original_names = {service["container_name"] for service in original["services"].values()}
            private_names = {service["container_name"] for service in private["services"].values()}
            self.assertTrue(original_names.isdisjoint(private_names))
            self.assertTrue(all(name.startswith(namespace + "-") for name in private_names))
            self.assertTrue(private_names.isdisjoint(service["container_name"] for service in provider["services"].values()))
            for service in private["services"].values():
                self.assertEqual(service["labels"]["io.nmrnet-repro.delivery"], namespace)
                for mount in service["volumes"]:
                    if mount["type"] == "volume":
                        self.assertIn(mount["source"], private["volumes"])
                        self.assertNotIn(mount["source"], original["volumes"])
            initialized = {mount["source"] for mount in private["services"]["init"]["volumes"]}
            self.assertEqual(initialized, set(private["volumes"]))

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
            arguments["bundle"] = second
            freeze_deployment(**arguments)
            self.assertEqual(previous, {path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()})
            self.assertNotEqual((first / "config/provider.toml").read_bytes(), (second / "config/provider.toml").read_bytes())
            self.assertEqual((second / "secrets/signing.private.json").read_bytes(), files.credential.read_bytes())
            self.assertEqual((second / "secrets/interpreter.key").read_bytes(), files.key.read_bytes())
            plan = json.loads((second / "compose.json").read_bytes())
            self.assertEqual(len(plan["services"]), 7)
            self.assertEqual(plan["name"], json.loads((first / "compose.json").read_bytes())["name"])
            self.assertEqual((second / "resolv.conf").read_text(), "nameserver 192.0.2.53\n")
            self.assertEqual((second / "execution.json").stat().st_mode & 0o777, 0o600)
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
                "config/provider.toml", "secrets/signing.private.json", "secrets/interpreter.key",
                "execution.json", "prediction-models.json", "inference.lock", "unicore-source.json",
                "compose.json", "resolv.conf",
            })


class ProviderNamespacePlanTests(ComposeFixture, unittest.TestCase):
    def test_production_default_plan_is_unchanged_and_private_checkout_changes_all_provider_names(self):
        default = self.plan()
        self.assertEqual(default, self.plan(delivery_project="nmr-api-delivery"))
        private = self.plan(checkout=self.root / "private-checkout", delivery_project="nmr-qualification-private")
        self.assertNotEqual(default["name"], private["name"])
        self.assertTrue(set(default["volumes"]).isdisjoint(private["volumes"]))
        self.assertEqual(set(default["services"]), set(private["services"]))
        self.assertTrue({service["container_name"] for service in default["services"].values()}.isdisjoint(
            service["container_name"] for service in private["services"].values()))
        for role in private["services"]:
            self.assertEqual(default["services"][role]["command"], private["services"][role]["command"])


class DeliveryApiSecretRecipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_reads_only_exact_api_secrets_without_a_model_key(self):
        async with TlsPeer("success") as peer:
            ca_bytes = (Path(peer.scratch.name) / "cert.pem").read_bytes()
            for explicit_ca in (False, True):
                with self.subTest(explicit_ca=explicit_ca), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    arguments, files = deployment_inputs(root)
                    uid, gid = os.geteuid(), os.getegid()
                    arguments.update(uid=uid, gid=gid)
                    if explicit_ca:
                        ca = files.root / "api-ca.pem"
                        ca.write_bytes(ca_bytes)
                        ca.chmod(0o600)
                        definition = arguments["definition"]
                        definition.write_bytes(definition.read_bytes().replace(
                            b"[api]\n", b'[api]\nca_file = "/srv/nmrnet/input/api-ca.pem"\n'))
                    freeze_deployment(**arguments)
                    bundle = arguments["bundle"]
                    signing_bytes = (bundle / "secrets/signing.private.json").read_bytes()
                    model_key = (bundle / "secrets/interpreter.key").read_bytes().strip()
                    # The new API-only owner must not read a model credential
                    # from either the original source or its sealed provider copy.
                    files.key.unlink()
                    (bundle / "secrets/interpreter.key").unlink()
                    bundle.chmod(0o500)
                    output = root / "delivery"
                    output.mkdir(mode=0o700)
                    prepare([bundle], output, uid, gid, arguments["controller_image_id"],
                            arguments["controller_image_id"])
                    (binding,) = (output / "bindings").iterdir()
                    expected = {"signing.json", "resolv.conf"} | ({"ca.pem"} if explicit_ca else set())
                    self.assertEqual({path.name for path in binding.iterdir()}, expected)
                    self.assertEqual((binding / "signing.json").read_bytes(), signing_bytes)
                    self.assertEqual((binding / "resolv.conf").read_bytes(), (bundle / "resolv.conf").read_bytes())
                    if explicit_ca:
                        self.assertEqual((binding / "ca.pem").read_bytes(), ca_bytes)
                    plan = json.loads((output / "compose.json").read_bytes())
                    daemon = plan["services"]["delivery"]
                    self.assertEqual({mount["target"]: mount["source"] for mount in daemon["volumes"] if mount["type"] == "bind"}, {
                        "/input": str(output), "/routes/" + binding.name: str(output / "runtime" / binding.name),
                    })
                    for service in plan["services"].values():
                        for mount in service["volumes"]:
                            if mount["type"] == "bind":
                                self.assertTrue(Path(mount["source"]).is_relative_to(output))
                                self.assertFalse(Path(mount["source"]).is_relative_to(bundle))
                    for artifact in output.rglob("*"):
                        if artifact.is_file():
                            self.assertNotIn(model_key, artifact.read_bytes())
                    outside = root / "outside-signing.json"
                    outside.write_bytes(signing_bytes)
                    outside.chmod(0o600)
                    config_path = bundle / "config/provider.toml"
                    config_path.write_text(config_path.read_text().replace(
                        str(bundle / "secrets/signing.private.json"), str(outside)))
                    refused = root / "refused"
                    refused.mkdir(mode=0o700)
                    with self.assertRaisesRegex(DeliveryDeploymentRejected, "original sealed secret leaf"):
                        prepare([bundle], refused, uid, gid, arguments["controller_image_id"],
                                arguments["controller_image_id"])
                    self.assertFalse((refused / "compose.json").exists())
                    self.assertEqual(peer.requests, [])
