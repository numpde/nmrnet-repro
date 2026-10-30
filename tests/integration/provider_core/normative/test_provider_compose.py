"""The deployment plan grants each fixed role only its actual runtime inputs."""

import unittest
import os
from dataclasses import replace
from tests.integration.nmrnet.compose_ports import ComposeFixture
from provider_core.producer_checkpoint import checkpoint_volume_name


class ComposePlanTests(ComposeFixture, unittest.TestCase):
    def test_fixed_roles_have_exact_images_resources_logging_and_private_grants(self):
        plan = self.plan()
        self.assertEqual(set(plan["services"]), {"provider", "prediction", "reader", "smiles", "interpreter", "store"})
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
        control = grants[str(self.root / "attempts")]
        self.assertEqual(control, {"type": "volume", "source": checkpoint_volume_name(self.config.lifecycle.scope),
                                   "target": str(self.root / "attempts"), "read_only": False, "volume": {"nocopy": True}})
        shared = self.grants.compose()
        for mount in shared["mounts"]:
            self.assertEqual(grants[mount["target"]], mount)
        self.assertEqual(provider["group_add"], [str(os.getegid())])
        self.assertEqual(plan["volumes"], shared["volumes"] | {control["source"]: {"name": control["source"], "external": True}})
        self.assertIn(str(self.grants.proxy_socket), provider["command"])
        for role, service in services.items():
            if role != "provider":
                self.assertFalse(any(grant["type"] == "volume" for grant in service["volumes"]))
        self.assertIn("/run:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid=1000,gid=1001", provider["tmpfs"])
        self.assertEqual(set(provider["depends_on"]), set(services) - {"provider"})
        self.assertGreater(int(provider["stop_grace_period"].removesuffix("s")), self.config.service.policy.drain_seconds + self.config.service.policy.settle_seconds)
        for role in ("prediction", "reader", "smiles"):
            command = services[role]["command"]
            budgets = {name: float(command[command.index("--" + name + "-seconds") + 1])
                       for name in ("startup", "terminate", "kill", "cleanup")}
            self.assertLessEqual(2 * budgets["startup"] + budgets["terminate"] + budgets["kill"] + budgets["cleanup"],
                                 float(services[role]["stop_grace_period"].removesuffix("s")))

    def test_prediction_profile_excludes_pre_acquisition_preparation_without_changing_client_budget(self):
        command = self.plan()["services"]["prediction"]["command"]
        io = float(command[command.index("--io-seconds") + 1])
        lifetime = float(command[command.index("--operation-seconds") + 1])
        self.assertGreater(io, 2 * self.config.service.native.prediction_seconds)
        self.config = replace(self.config, preparation=replace(self.config.preparation, seconds=45000))
        changed = self.plan()["services"]["prediction"]["command"]
        self.assertEqual(float(changed[changed.index("--io-seconds") + 1]), io)
        self.assertEqual(io, lifetime)
        self.assertEqual(self.config.service.native.prediction_seconds, 5)

    def test_project_identity_survives_bundle_refresh_with_shared_grants(self):
        first = self.plan()
        second = self.plan(bundle=self.bundle.parent / "generation-second")
        self.assertEqual(first["name"], second["name"])
        self.assertNotEqual(first["services"]["provider"]["labels"], second["services"]["provider"]["labels"])
        self.assertEqual(first["volumes"], second["volumes"])
        self.assertEqual(first["services"]["provider"]["command"][-2:], second["services"]["provider"]["command"][-2:])
