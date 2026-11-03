"""Acquisition must reject routing ambiguity before invoking the engine."""

import os
import shlex
import subprocess
import unittest


class NetworkGateTests(unittest.TestCase):
    def invoke(self, script, lane, interface=None):
        environment = dict(os.environ)
        # silent-defaults: allow-pop-default -- Remove an inherited forwarded interface so omission cannot authorize routing; proof=tests/test_network_gate.py::NetworkGateTests.test_networked_lanes_require_an_explicit_interface
        environment.pop("NMRNET_NETWORK_INTERFACE_INPUT", None)
        # silent-defaults: allow-pop-default -- Remove the ambient interface before checking that routing requires an explicit selector; proof=tests/test_network_gate.py::NetworkGateTests.test_networked_lanes_require_an_explicit_interface
        environment.pop("NETWORK_INTERFACE", None)
        environment["NMRNET_ARCHIVE_INPUT"] = "weight.zip"
        environment["NMRNET_WHEELHOUSE_INPUT"] = "/tmp"
        environment["NMRNET_WHEELHOUSE_INPUT_ORIGIN"] = "command line"
        environment["NMRNET_DOCKER_CONTEXT_INPUT"] = "fixture"
        environment["NMRNET_DOCKER_CONTEXT_INPUT_ORIGIN"] = "command line"
        environment["NMRNET_DOCKER_ENDPOINT_INPUT"] = "unix:///fixture.sock"
        environment["NMRNET_SHARED_RUNTIME_IMAGE_INPUT"] = "sha256:" + "a" * 64
        if interface is not None:
            environment["NMRNET_NETWORK_INTERFACE_INPUT"] = interface
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
                self.assertIn("NETWORK_INTERFACE", result.stderr)
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

    def test_acquisition_preview_shows_separated_worker_and_selected_route(self):
        result = self.invoke("container", "download", "lo")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertIn("acquisition route: lo", result.stdout)
        self.assertIn("fixture (unix:///fixture.sock)", result.stdout)
        worker = shlex.split(result.stdout.split("Online worker: ", 1)[1])
        self.assertIn("--read-only", worker)
        self.assertIn("--cap-drop", worker)
        self.assertIn("NMRNET_IN_CONTAINER=1", worker)
        self.assertIn("--", worker)
        self.assertNotIn("--network", worker)
        self.assertNotIn("--user", worker)
        self.assertNotIn("docker.sock", " ".join(worker))

    def test_daemon_pull_lanes_are_not_supported(self):
        for script in ("container", "packages"):
            with self.subTest(script=script):
                result = self.invoke(script, "pull", "lo")
                self.assertNotEqual(0, result.returncode)
                self.assertEqual("", result.stdout)
