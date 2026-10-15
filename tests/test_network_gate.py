"""Acquisition must reject routing ambiguity before invoking the engine."""

import os
import shlex
import subprocess
import unittest
from pathlib import Path


class NetworkGateTests(unittest.TestCase):
    def invoke(self, script, lane, interface=None):
        environment = dict(os.environ)
        # silent-defaults: allow-pop-default -- Remove an inherited forwarded interface so omission cannot authorize routing; proof=tests/test_network_gate.py::NetworkGateTests.test_networked_lanes_require_an_explicit_interface
        environment.pop("NMRNET_WLAN_INTERFACE_INPUT", None)
        # silent-defaults: allow-pop-default -- Remove the ambient interface before checking that routing requires an explicit selector; proof=tests/test_network_gate.py::NetworkGateTests.test_networked_lanes_require_an_explicit_interface
        environment.pop("WLAN_INTERFACE", None)
        environment["NMRNET_ARCHIVE_INPUT"] = "weight.zip"
        if interface is not None:
            environment["NMRNET_WLAN_INTERFACE_INPUT"] = interface
        return subprocess.run(
            ["bash", f"tools/{script}.sh", "--print", lane],
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_networked_lanes_require_an_explicit_interface(self):
        for script, lane in (("container", "download"), ("packages", "stage")):
            with self.subTest(script=script, lane=lane):
                result = self.invoke(script, lane)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("WLAN_INTERFACE", result.stderr)
                self.assertEqual("", result.stdout)

    def test_networked_lanes_reject_an_unavailable_interface(self):
        for script, lane in (("container", "download"), ("packages", "stage")):
            with self.subTest(script=script, lane=lane):
                result = self.invoke(script, lane, "nmrnet_missing0")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("nmrnet_missing0", result.stderr)
                self.assertEqual("", result.stdout)

    def test_offline_lane_does_not_need_or_consume_the_interface(self):
        result = self.invoke("container", "test", "nmrnet_missing0")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("--network none", result.stdout)
        self.assertNotIn("nmrnet_missing0", result.stdout)

    def test_daemon_pulls_are_rejected_without_engine_access(self):
        for script in ("container", "packages"):
            with self.subTest(script=script):
                result = self.invoke(script, "pull", "lo")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("preloaded", result.stderr)
                self.assertEqual("", result.stdout)

    def test_proxy_and_worker_are_contained_and_override_proxy_environment(self):
        result = self.invoke("container", "download", "lo")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        proxy, start_proxy, worker, start_worker = [shlex.split(line) for line in result.stdout.splitlines()]
        self.assertEqual(["start", "PROXY_ID"], start_proxy[-2:])
        self.assertEqual(["start", "--attach", "WORKER_ID"], start_worker[-3:])
        for command in (proxy, worker):
            self.assertIn("create", command)
            self.assertNotIn("--rm", command)
            self.assertIn("--read-only", command)
            self.assertIn("no-new-privileges:true", command)
            self.assertEqual(f"{os.getuid()}:{os.getgid()}", command[command.index("--user") + 1])
            self.assertEqual("ALL", command[command.index("--cap-drop") + 1])
            self.assertEqual("never", command[command.index("--pull") + 1])
            self.assertIn("NMRNET_IN_CONTAINER=1", command)
            for limit in ("--cpus", "--memory", "--memory-swap", "--pids-limit"):
                self.assertIn(limit, command)
            mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "--mount"]
            self.assertFalse(any("docker.sock" in mount or "dst=/home" in mount for mount in mounts))
        self.assertIn("python3", proxy)
        self.assertEqual("tools.bound_http_proxy", proxy[proxy.index("-m") + 1])
        self.assertEqual("/workspace", proxy[proxy.index("--workdir") + 1])
        proxy_mounts = [proxy[i + 1] for i, arg in enumerate(proxy) if arg == "--mount"]
        self.assertEqual(
            {
                f"type=bind,src={Path.cwd()}/tools/bound_http_proxy.py,dst=/workspace/tools/bound_http_proxy.py,readonly",
                f"type=bind,src={Path.cwd()}/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly",
                "type=bind,src=/tmp/nmrnet-proxy.PRINT/ready,dst=/ready",
            },
            set(proxy_mounts),
        )
        self.assertIn("type=bind,src=/tmp/nmrnet-proxy.PRINT/ready,dst=/ready", proxy)
        self.assertNotIn("type=bind,src=/tmp/nmrnet-proxy.PRINT,dst=/ready", proxy)
        self.assertEqual("lo", proxy[proxy.index("--interface") + 1])
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertIn(f"{name}=http://127.0.0.1:PORT", worker)
        for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
            self.assertIn(f"{name}=", worker)
