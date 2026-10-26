"""Unsafe identity and destination grants never produce a deployable plan."""

import unittest
from dataclasses import replace
from pathlib import Path

from nmrnet_provider.config import ConfigurationRejected
from tests.integration.nmrnet.compose_ports import ComposeFixture


class ComposeRefusalTests(ComposeFixture, unittest.TestCase):
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
            if route:
                route = dict(route, log_level="INFO")
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
