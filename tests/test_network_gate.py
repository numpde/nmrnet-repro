"""Acquisition must reject routing ambiguity before invoking the engine."""

import os
import shlex
import subprocess
import unittest


class NetworkGateTests(unittest.TestCase):
    def invoke(self, script, lane, interface=None):
        environment = dict(os.environ)
        environment.pop("NMRNET_WLAN_INTERFACE_INPUT", None)
        environment.pop("WLAN_INTERFACE", None)
        environment["ARCHIVE"] = "weight.zip"
        if interface is not None:
            environment["NMRNET_WLAN_INTERFACE_INPUT"] = interface
        return subprocess.run(
            ["bash", f"tools/{script}.sh", "--print", lane],
            env=environment, text=True, capture_output=True,
        )

    def test_networked_lanes_require_an_explicit_interface(self):
        for script, lane in (("container", "download"), ("packages", "stage")):
            with self.subTest(script=script, lane=lane):
                result = self.invoke(script, lane)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("WLAN_INTERFACE", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_networked_lanes_reject_an_unavailable_interface(self):
        for script, lane in (("container", "download"), ("packages", "stage")):
            with self.subTest(script=script, lane=lane):
                result = self.invoke(script, lane, "nmrnet_missing0")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("nmrnet_missing0", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_offline_lane_does_not_need_or_consume_the_interface(self):
        result = self.invoke("container", "test", "nmrnet_missing0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--network none", result.stdout)
        self.assertNotIn("nmrnet_missing0", result.stdout)

    def test_daemon_pulls_are_rejected_without_engine_access(self):
        for script in ("container", "packages"):
            with self.subTest(script=script):
                result = self.invoke(script, "pull", "lo")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("preloaded", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_proxy_and_worker_are_contained_and_override_proxy_environment(self):
        result = self.invoke("container", "download", "lo")
        self.assertEqual(result.returncode, 0, result.stderr)
        proxy, start_proxy, worker, start_worker = [shlex.split(line) for line in result.stdout.splitlines()]
        self.assertEqual(start_proxy[-2:], ["start", "PROXY_ID"])
        self.assertEqual(start_worker[-3:], ["start", "--attach", "WORKER_ID"])
        for command in (proxy, worker):
            self.assertIn("create", command)
            self.assertNotIn("--rm", command)
            self.assertIn("--read-only", command)
            self.assertIn("no-new-privileges:true", command)
            self.assertEqual(command[command.index("--user") + 1], f"{os.getuid()}:{os.getgid()}")
            self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
            self.assertEqual(command[command.index("--pull") + 1], "never")
            for limit in ("--cpus", "--memory", "--memory-swap", "--pids-limit"):
                self.assertIn(limit, command)
            mounts = [command[i + 1] for i, arg in enumerate(command) if arg == "--mount"]
            self.assertFalse(any("docker.sock" in mount or "dst=/home" in mount for mount in mounts))
        self.assertIn("python3", proxy)
        self.assertIn("/proxy.py", proxy)
        self.assertIn("type=bind,src=/tmp/nmrnet-proxy.PRINT/ready,dst=/ready", proxy)
        self.assertNotIn("type=bind,src=/tmp/nmrnet-proxy.PRINT,dst=/ready", proxy)
        self.assertEqual(proxy[proxy.index("--interface") + 1], "lo")
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertIn(f"{name}=http://127.0.0.1:PORT", worker)
        for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
            self.assertIn(f"{name}=", worker)
