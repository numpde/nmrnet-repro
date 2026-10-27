"""Prove the contained proxy receives only its optional configured upstream file.

The host proxy precedent reads this exact file itself:
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L141
These engine-free checks qualify file authority, not DNS reachability. An owned
launcher copy maps its one fixed host input to temporary fixture storage; the
production input has no operator override and its container destination stays
unchanged. Actual resolver contents are never read or copied by the launcher.
"""

from tools.container_guard import require_container

require_container()

import os
import shlex
import tempfile
import unittest
from pathlib import Path

from repository_checks.fixtures.containers.fixture import LauncherFixture
from repository_checks.quality.sources import ROOT


class ProxyResolverInputTests(unittest.TestCase):
    def mapped_fixture(self, root):
        fixture = LauncherFixture(root)
        resolver = root / "configured-resolver"
        launcher = root / "tools/docker.sh"
        source = launcher.read_text()
        declaration = "readonly resolver_source='/run/systemd/resolve/resolv.conf'"
        self.assertEqual(1, source.count(declaration), msg="The fixture must map exactly one production-owned host path")
        launcher.write_text(source.replace(declaration, "readonly resolver_source=" + shlex.quote(str(resolver)), 1))
        return fixture, resolver

    def proxy_and_worker(self, fixture):
        creations = [call["arguments"] for call in fixture.calls() if call["arguments"][:1] == ["create"]]
        self.assertEqual(2, len(creations))
        return creations

    def mounts(self, command):
        return [command[index + 1] for index, value in enumerate(command) if value == "--mount"]

    def test_safe_optional_resolver_adds_one_readonly_proxy_grant_and_keeps_worker_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, resolver = self.mapped_fixture(Path(directory))
            resolver.write_text("private-resolver-content-canary\n")
            result = fixture.run(
                "docker.sh", "run", "sha256:" + "b" * 64, "true", additions={"NMRNET_NETWORK_INTERFACE_INPUT": "lo"}
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            proxy, worker = self.proxy_and_worker(fixture)
            mounts = self.mounts(proxy)
            grant = "type=bind,src=" + str(resolver) + ",dst=/run/systemd/resolve/resolv.conf,readonly"
            self.assertEqual(1, mounts.count(grant))
            self.assertEqual(4, len(mounts))
            self.assertEqual(3, sum(mount.endswith(",readonly") for mount in mounts))
            self.assertEqual([], self.mounts(worker))
            self.assertIn("--read-only", proxy)
            self.assertEqual("host", proxy[proxy.index("--network") + 1])
            self.assertEqual("ALL", proxy[proxy.index("--cap-drop") + 1])
            self.assertEqual(str(os.getuid()) + ":" + str(os.getgid()), proxy[proxy.index("--user") + 1])
            self.assertNotIn("private-resolver-content-canary", result.stdout + result.stderr)

    def test_absent_resolver_keeps_existing_three_proxy_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, resolver = self.mapped_fixture(Path(directory))
            self.assertFalse(resolver.exists())
            result = fixture.run(
                "docker.sh", "run", "sha256:" + "b" * 64, "true", additions={"NMRNET_NETWORK_INTERFACE_INPUT": "lo"}
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            proxy, worker = self.proxy_and_worker(fixture)
            mounts = self.mounts(proxy)
            self.assertEqual(3, len(mounts))
            self.assertFalse(any(",dst=/run" in mount or ",dst=/etc" in mount for mount in mounts))
            self.assertEqual([], self.mounts(worker))

    def test_unsafe_existing_resolver_rejects_before_any_engine_call(self):
        for kind in ("symlink", "dangling-link", "directory", "fifo", "unreadable"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                fixture, resolver = self.mapped_fixture(Path(directory))
                if kind == "symlink":
                    outside = fixture.root / "outside-config"
                    outside.write_text("private-resolver-content-canary\n")
                    resolver.symlink_to(outside)
                elif kind == "dangling-link":
                    resolver.symlink_to(fixture.root / "missing-config")
                elif kind == "directory":
                    resolver.mkdir()
                elif kind == "fifo":
                    os.mkfifo(resolver)
                else:
                    resolver.write_text("private-resolver-content-canary\n")
                    resolver.chmod(0)
                result = fixture.run(
                    "docker.sh", "run", "sha256:" + "b" * 64, "true", additions={"NMRNET_NETWORK_INTERFACE_INPUT": "lo"}
                )
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertIn("readable regular file without a symlink", result.stderr)
                self.assertEqual([], fixture.calls())
                self.assertNotIn("private-resolver-content-canary", result.stdout + result.stderr)

    def test_interface_is_still_explicit_even_when_resolver_input_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, resolver = self.mapped_fixture(Path(directory))
            resolver.write_text("private-resolver-content-canary\n")
            result = fixture.run("docker.sh", "run", "sha256:" + "b" * 64, "true", additions={"NMRNET_NETWORK_INTERFACE_INPUT": ""})
            self.assertEqual(2, result.returncode)
            self.assertIn("supply NETWORK_INTERFACE=", result.stderr)
            self.assertEqual([], fixture.calls())

    def test_production_host_input_is_one_fixed_literal_without_an_operator_override(self):
        source = (ROOT / "tools/docker.sh").read_text()
        self.assertEqual(1, source.count("readonly resolver_source='/run/systemd/resolve/resolv.conf'"))
        self.assertNotIn("RESOLVER_INPUT", source)
