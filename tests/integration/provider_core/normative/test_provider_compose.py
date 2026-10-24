"""The deployment plan grants each fixed role only its actual runtime inputs."""

import unittest
import json
from tests.integration.nmrnet.compose_ports import ComposeFixture
from nmrnet_provider.deployment import inventory_containers, admit_container, compose_bytes
from tools.provider_deployment import admit_network, deployment_manifest, inspect_deployment
from nmrnet_provider.config import ConfigurationRejected


class ComposePlanTests(ComposeFixture, unittest.TestCase):
    def test_host_manifest_and_local_peer_admission_keep_exact_network_identity(self):
        self.routes["api"] = {"mode": "local", "interface": "eth0", "network": "a" * 64,
                              "addresses": ["api.example=172.22.0.2"]}
        plan = self.plan()
        (self.bundle / "compose.json").write_bytes(compose_bytes(plan))
        (self.bundle / "compose.json").chmod(0o400)
        output = self.root / "manifest"
        deployment_manifest(bundle=self.bundle, output=output)
        fields = output.read_bytes().split(b"\0")[:-1]
        self.assertEqual(len(fields), 20)
        self.assertEqual(fields[4:7], [b"local", b"eth0", b"a" * 64])
        self.assertGreater(int(fields[-1]), 0)
        self.assertNotIn(b"PRIVATE_MODEL_KEY", output.read_bytes())
        observation = [{"Id": "a" * 64, "Driver": "bridge", "Internal": True,
                        "Containers": {"peer": {"IPv4Address": "172.22.0.2/16"}}}]
        inspection = self.root / "network.json"
        inspection.write_text(json.dumps(observation))
        admit_network(bundle=self.bundle, role="api", inspection=inspection)
        for change in ("identity", "public", "address", "netmask"):
            with self.subTest(change=change):
                changed = json.loads(json.dumps(observation))
                if change == "identity":
                    changed[0]["Id"] = "b" * 64
                elif change == "public":
                    changed[0]["Internal"] = False
                elif change == "address":
                    changed[0]["Containers"]["peer"]["IPv4Address"] = "172.22.0.3/16"
                else:
                    changed[0]["Containers"]["peer"]["IPv4Address"] = "172.22.0.2/99"
                inspection.write_text(json.dumps(changed))
                with self.assertRaises(ConfigurationRejected):
                    admit_network(bundle=self.bundle, role="api", inspection=inspection)

    def test_contained_inspection_emits_only_admitted_control_fields(self):
        record, image = self.grant_record()
        record["State"]["Status"] = "created"
        inspection, identifiers, images = (self.root / name for name in ("inspection.json", "ids", "images.json"))
        inspection.write_text(json.dumps([record]))
        identifiers.write_text(record["Id"] + "\n")
        images.write_text(json.dumps([image]))
        plan = self.bundle / "compose.json"
        plan.write_bytes(compose_bytes(self.plan()))
        plan.chmod(0o400)
        complete = self.bundle / "complete"
        complete.write_bytes(b"complete\n")
        complete.chmod(0o400)
        self.bundle.chmod(0o500)
        output = self.root / "admitted-fields"
        inspect_deployment(inspection=inspection, identifiers=identifiers, images=images,
                           checkout=self.root / "checkout", deployment="web", bundles=self.bundle.parent, output=output)
        fields = output.read_bytes().split(b"\0")
        self.assertEqual(fields[:8], [b"provider", record["Id"].encode(), b"generation-first", image["Id"].encode(),
                                      b"created", b"false", b"false", b"0"])
        self.assertEqual(fields[-1], b"")
        self.assertEqual(len(fields), 10)
        self.assertNotIn(b"PATH", output.read_bytes())
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_created_and_stopped_provider_and_reader_match_their_original_grants(self):
        plan = self.plan()
        for role in ("provider", "reader"):
            with self.subTest(role=role):
                record, image = self.grant_record(role=role)
                admit_container(record, role=role, original_plan=plan, image=image)
                record["HostConfig"]["SecurityOpt"] = ["no-new-privileges"]
                record["HostConfig"]["Tmpfs"]["/tmp"] = "gid=1001,uid=1000,mode=700,size=67108864,noexec,nodev,nosuid,rw"
                admit_container(record, role=role, original_plan=plan, image=image)

    def test_actual_stopped_subset_keeps_original_bundle_when_another_is_selected(self):
        original = self.container_record(bundle="generation-original")
        replacement = self.plan(bundle=self.bundle.parent / "generation-replacement")
        actual = inventory_containers([original], requested_ids=[original["Id"]], checkout=self.root / "checkout", deployment="web")
        self.assertEqual(set(actual), {"provider"})
        self.assertIs(actual["provider"], original)
        self.assertEqual(actual["provider"]["Config"]["Labels"]["io.nmrnet-repro.bundle"], "generation-original")
        self.assertNotEqual(replacement["services"]["provider"]["labels"]["io.nmrnet-repro.bundle"], "generation-original")
        self.assertEqual(inventory_containers([], requested_ids=[], checkout=self.root / "checkout", deployment="web"), {})


    def test_fixed_roles_have_exact_images_resources_logging_and_private_grants(self):
        plan = self.plan()
        self.assertEqual(set(plan["services"]), {"provider", "prediction", "reader", "smiles", "api", "interpreter", "store"})
        for role, service in plan["services"].items():
            self.assertTrue(service["read_only"])
            self.assertTrue(service["init"])
            self.assertEqual(service["user"], "1000:1001")
            self.assertEqual(service["cap_drop"], ["ALL"])
            self.assertEqual(service["security_opt"], ["no-new-privileges:true"])
            self.assertEqual(service["cpus"], 2)
            self.assertEqual(service["pids_limit"], 64)
            self.assertEqual(service["restart"], "unless-stopped")
            self.assertGreater(int(service["stop_grace_period"].removesuffix("s")), 0)
            self.assertEqual(service["logging"], {"driver": "json-file", "options": {
                "max-size": "10m", "max-file": "3", "mode": "non-blocking", "max-buffer-size": "1m"}})
            self.assertEqual(service["mem_limit"], "4g" if role == "prediction" else "512m")
            self.assertEqual(service["memswap_limit"], service["mem_limit"])
            self.assertNotIn("/workspace", str(service))
            self.assertNotIn("docker.sock", str(service))
        services = plan["services"]
        self.assertEqual(services["prediction"]["image"], self.execution["image_id"])
        self.assertEqual(services["reader"]["image"], self.execution["geometry_image_id"])
        for role in ("prediction", "reader", "smiles"):
            service = services[role]
            self.assertEqual(service["network_mode"], "none")
            grants = {item["target"]: item for item in service["volumes"]}
            self.assertFalse(grants[f"/native/{role}"]["read_only"])
            self.assertTrue(grants[f"/native/{role}"]["source"].endswith(f"/runtime/{role}"))
            self.assertNotIn(str(self.files.credential), str(service))
            self.assertIn("/scratch:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid=1000,gid=1001", service["tmpfs"])
        provider = services["provider"]
        grants = {item["target"]: item for item in provider["volumes"]}
        for role in ("prediction", "reader", "smiles"):
            self.assertTrue(grants[f"/native/{role}"]["read_only"])
        self.assertEqual(provider["network_mode"], "none")
        self.assertFalse(grants[str(self.root / "attempts")]["read_only"])
        self.assertIn("/run:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid=1000,gid=1001", provider["tmpfs"])
        self.assertEqual(set(provider["depends_on"]), set(services) - {"provider"})
        self.assertGreater(int(provider["stop_grace_period"].removesuffix("s")), self.config.service.policy.drain_seconds + self.config.service.policy.settle_seconds)
        for role in ("prediction", "reader", "smiles"):
            command = services[role]["command"]
            budgets = {name: float(command[command.index("--" + name + "-seconds") + 1])
                       for name in ("startup", "terminate", "kill", "cleanup")}
            self.assertLessEqual(2 * budgets["startup"] + budgets["terminate"] + budgets["kill"] + budgets["cleanup"],
                                 float(services[role]["stop_grace_period"].removesuffix("s")))

    def test_prediction_profile_includes_pre_science_wait_without_changing_client_budget(self):
        command = self.plan()["services"]["prediction"]["command"]
        io = float(command[command.index("--io-seconds") + 1])
        lifetime = float(command[command.index("--operation-seconds") + 1])
        self.assertGreater(io, 2 * self.config.preparation.seconds + 2 * self.config.service.native.prediction_seconds)
        self.assertEqual(io, lifetime)
        self.assertEqual(self.config.service.native.prediction_seconds, 5)

    def test_project_identity_survives_bundle_refresh_and_routes_preserve_exact_authority(self):
        first = self.plan()
        second = self.plan(bundle=self.bundle.parent / "generation-second")
        self.assertEqual(first["name"], second["name"])
        self.assertNotEqual(first["services"]["provider"]["labels"], second["services"]["provider"]["labels"])
        self.routes["api"] = {"mode": "mapped", "interface": "eno1", "addresses": ["api.example=192.0.2.12"]}
        api = self.plan()["services"]["api"]
        self.assertEqual(api["network_mode"], "host")
        self.assertIn("api.example:443", api["command"])
        self.assertIn("api.example=192.0.2.12", api["command"])
        self.assertIn("eno1", api["command"])
        self.assertNotIn("192.0.2.12:443", api["command"])

    def test_local_proxy_joins_only_its_explicit_network_without_host_network_fallback(self):
        self.routes["api"] = {"mode": "local", "interface": "eth0", "addresses": ["api.example=192.0.2.12"], "network": "a" * 64}
        plan = self.plan()
        api = plan["services"]["api"]
        self.assertNotIn("network_mode", api)
        self.assertEqual(api["networks"], ["api"])
        self.assertEqual(plan["networks"], {"api": {"external": True, "name": "a" * 64}})
        self.assertIn("api.example:443", api["command"])
        self.assertNotIn("/etc/resolv.conf", str(api["volumes"]))
        self.assertEqual(plan["services"]["provider"]["network_mode"], "none")
