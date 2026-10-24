"""Unsafe identity and destination grants never produce a deployable plan."""

import unittest
import copy
from dataclasses import replace
from pathlib import Path

from nmrnet_provider.config import ConfigurationRejected
from nmrnet_provider.deployment import inventory_containers, admit_container
from tests.integration.nmrnet.compose_ports import ComposeFixture


class ComposeRefusalTests(ComposeFixture, unittest.TestCase):
    def test_changed_creation_grants_refuse_control_instead_of_accepting_expected_subset(self):
        original, image = self.grant_record()
        changes = []
        for name, value in (("ReadonlyRootfs", False), ("Privileged", True), ("Init", False), ("Memory", 0),
                            ("MemorySwap", -1), ("NanoCpus", 0), ("PidsLimit", 0), ("CapDrop", []), ("CapAdd", ["SYS_ADMIN"]),
                            ("SecurityOpt", []), ("PidMode", "host"), ("UTSMode", "host"), ("UsernsMode", "host"), ("IpcMode", "host"),
                            ("CgroupnsMode", "host"), ("Devices", [{"PathOnHost": "/dev/null"}]),
                            ("ExtraHosts", ["unexpected:192.0.2.1"]), ("Dns", ["192.0.2.53"]), ("PublishAllPorts", True),
                            ("Sysctls", {"net.ipv4.ip_forward": "1"}), ("MaskedPaths", []), ("ReadonlyPaths", []),
                            ("PortBindings", {"80/tcp": [{"HostPort": "80"}]}), ("NetworkMode", "host")):
            changed = copy.deepcopy(original)
            changed["HostConfig"][name] = value
            changes.append(changed)
        for name, value in (("User", "0:0"), ("Entrypoint", ["/bin/sh"]), ("Cmd", ["echo", "PRIVATE"]),
                            ("Env", [*original["Config"]["Env"], "PRIVATE_CANARY=secret"]), ("StopTimeout", 0),
                            ("StopSignal", "SIGKILL"), ("WorkingDir", "/tmp"), ("OpenStdin", True), ("Tty", True)):
            changed = copy.deepcopy(original)
            changed["Config"][name] = value
            changes.append(changed)
        changed = copy.deepcopy(original)
        changed["Mounts"].append({"Type": "bind", "Source": "/", "Destination": "/extra", "RW": True})
        changes.append(changed)
        changed = copy.deepcopy(original)
        changed["Mounts"][4]["RW"] = True
        changes.append(changed)
        changed = copy.deepcopy(original)
        changed["HostConfig"]["Tmpfs"]["/diagnostics"] = "rw,size=64m"
        changes.append(changed)
        changed = copy.deepcopy(original)
        changed["HostConfig"]["LogConfig"]["Config"]["mode"] = "blocking"
        changes.append(changed)
        changed = copy.deepcopy(original)
        changed["HostConfig"]["RestartPolicy"]["Name"] = "always"
        changes.append(changed)
        changed = copy.deepcopy(original)
        changed["NetworkSettings"]["Networks"]["extra"] = {"NetworkID": "a" * 64}
        changes.append(changed)
        for record in changes:
            with self.subTest(record=record), self.assertRaises(ConfigurationRejected) as caught:
                admit_container(record, role="provider", original_plan=self.plan(), image=image)
            self.assertNotIn("PRIVATE_CANARY", str(caught.exception))
        changed = copy.deepcopy(original)
        changed["HostConfig"]["MemorySwap"] = -1
        with self.assertRaisesRegex(ConfigurationRejected, "provider HostConfig.MemorySwap.*536870912.*-1"):
            admit_container(changed, role="provider", original_plan=self.plan(), image=image)
        changed = copy.deepcopy(original)
        changed["Config"]["Env"].append("PRIVATE_CANARY=secret")
        with self.assertRaisesRegex(ConfigurationRejected, "provider Config.Env.*1 extra, 0 missing, 0 changed"):
            admit_container(changed, role="provider", original_plan=self.plan(), image=image)
        reader, image = self.grant_record(role="reader")
        del reader["HostConfig"]["Tmpfs"]["/scratch"]
        reader["Mounts"].append({"Type": "bind", "Source": "/private-scratch", "Destination": "/scratch", "RW": True})
        with self.assertRaises(ConfigurationRejected):
            admit_container(reader, role="reader", original_plan=self.plan(), image=image)
        reader, image = self.grant_record(role="reader")
        with self.assertRaisesRegex(ConfigurationRejected, "original creation generation"):
            admit_container(reader, role="reader", original_plan=self.plan(bundle=self.bundle.parent / "generation-replacement"), image=image)

    def test_inventory_refuses_foreign_duplicate_and_incomplete_engine_observations(self):
        original = self.container_record()
        mutations = []
        for name in ("com.docker.compose.project", "com.docker.compose.service", "com.docker.compose.oneoff",
                     "com.docker.compose.project.working_dir", "io.nmrnet-repro.checkout", "io.nmrnet-repro.deployment",
                     "io.nmrnet-repro.bundle"):
            changed = copy.deepcopy(original)
            changed["Config"]["Labels"][name] = "../foreign"
            mutations.append([changed])
        for name, value in (("Running", 1), ("Restarting", None), ("Pid", True)):
            changed = copy.deepcopy(original)
            changed["State"][name] = value
            mutations.append([changed])
        changed = copy.deepcopy(original)
        changed["Id"] = "2" * 64
        mutations.extend(([changed], [], [original, original]))
        for records in mutations:
            with self.subTest(records=records), self.assertRaises(ConfigurationRejected):
                inventory_containers(records, requested_ids=[original["Id"]], checkout=self.root / "checkout", deployment="web")
        other = self.container_record(identity="2" * 64)
        with self.assertRaisesRegex(ConfigurationRejected, "duplicate service role"):
            inventory_containers([original, other], requested_ids=[original["Id"], other["Id"]], checkout=self.root / "checkout", deployment="web")

    def test_missing_extra_and_malformed_image_facts_are_refused(self):
        for mutation in ("missing", "private", "identity"):
            with self.subTest(mutation=mutation):
                execution = dict(self.execution)
                if mutation == "missing":
                    del execution["image_id"]
                elif mutation == "private":
                    execution["PRIVATE_CANARY"] = "credential"
                else:
                    execution["image_id"] = "mutable:latest"
                with self.assertRaises(ConfigurationRejected):
                    self.plan(execution=execution)

    def test_routes_require_interface_exact_members_and_applicable_address_grants(self):
        cases = (
            {}, {"mode": "external", "interface": ""},
            {"mode": "external", "interface": "eth0", "addresses": ["api.example=192.0.2.1"]},
            {"mode": "mapped", "interface": "eth0", "addresses": []},
            {"mode": "mapped", "interface": "eth0", "addresses": ["other.example=192.0.2.1"]},
            {"mode": "external", "interface": "eth0", "network": "a" * 64},
            {"mode": "local", "interface": "eth0", "addresses": ["api.example=192.0.2.1"], "network": "unobserved-name"},
            {"mode": "external", "interface": "eth0", "unused": "PRIVATE_CANARY"},
        )
        for route in cases:
            with self.subTest(route=route), self.assertRaises(ConfigurationRejected):
                self.plan(routes={**self.routes, "api": route})
        with self.assertRaises(ConfigurationRejected):
            self.plan(routes={**self.routes, "extra": self.routes["api"]})

    def test_writable_store_cannot_mask_runtime_inputs_or_image_software(self):
        for path in (self.bundle, self.bundle.parent.parent / "runtime", self.root / "prepared", self.root / "checkout",
                     Path("/run"), Path("/run/state"), Path("/run/status.json")):
            with self.subTest(path=path), self.assertRaises(ConfigurationRejected):
                self.config = replace(self.config, lifecycle=replace(self.config.lifecycle, store_root=path))
                self.plan()

    def test_nonroot_identity_and_bundle_ownership_are_required(self):
        for changes in ({"uid": 0}, {"gid": 0}, {"uid": True}, {"deployment": "../web"},
                        {"bundle": self.root / "other/bundles/generation-first"}):
            with self.subTest(changes=changes), self.assertRaises(ConfigurationRejected):
                self.plan(**changes)
