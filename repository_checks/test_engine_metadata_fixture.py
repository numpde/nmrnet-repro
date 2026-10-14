"""Keep engine metadata projection finite for current launcher integration.

tools/baseline.sh admits image ID and upstream revision in one observation;
tools/inference_image.sh retains its ID projection. These are contained engine
composition proofs; native Git and real engine identity proofs remain separate.
"""

from tools.container_guard import require_container

require_container()

import subprocess
import tempfile
import unittest
from pathlib import Path

from repository_checks.fixtures.containers.fixture import LauncherFixture


class EngineMetadataFixtureTests(unittest.TestCase):
    def inspect(self, fixture, arguments, additions):
        return subprocess.run(
            [str(fixture.root / "bin/docker"), *arguments],
            env=fixture.environment | additions,
            capture_output=True,
            check=False,
        )

    def test_exact_metadata_projections_retain_literal_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            identity = "sha256:" + "c" * 64
            revision = "d" * 40
            label = '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}'
            forms = (
                (["image", "inspect", identity], identity),
                (["image", "inspect", "--format", "{{.Id}}", identity], identity),
                (["image", "inspect", "--format", label, identity], revision),
                (["image", "inspect", "--format", "{{.Id}}|" + label, identity], identity + "|" + revision),
            )
            for arguments, expected in forms:
                with self.subTest(arguments=arguments):
                    result = self.inspect(
                        fixture,
                        arguments,
                        {
                            "NMRNET_FAKE_IMAGE_ID": identity,
                            "NMRNET_FAKE_IMAGE_REVISION": revision,
                        },
                    )
                    self.assertEqual(0, result.returncode, msg=result.stderr)
                    self.assertEqual((expected + "\n").encode(), result.stdout)
                    self.assertEqual(b"", result.stderr)
            self.assertEqual(113, len(result.stdout))

    def test_unknown_formats_and_nonexact_argv_reject_before_fault_responses(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            identity = "sha256:" + "b" * 64
            label = '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}'
            requests = (
                ["image", "inspect", "--format", "contains-upstream-revision", identity],
                ["image", "inspect", "--format", label + "suffix", identity],
                ["image", "inspect", "--format", "{{.Id}}|" + label + "\n", identity],
                ["image", "inspect", "--format", "{{.Id}}|" + label + "|{{.Name}}", identity],
                ["image", "inspect", "--format {{.Id}}", identity],
                ["image", "inspect", "--format", "{{.Id}}", ""],
                ["image", "inspect", "--format", "{{.Id}}", identity, "extra"],
                ["image", "inspect", identity, "extra"],
                ["image", "inspect", "--format", "{{.Id}}", "--unexpected"],
            )
            private = "private-unmodeled-inspection-canary"
            for arguments in requests:
                with self.subTest(arguments=arguments):
                    result = self.inspect(
                        fixture,
                        arguments,
                        {
                            "NMRNET_FAKE_INSPECT_STATUS": "7",
                            "NMRNET_FAKE_INSPECT_PRIVATE_ERROR": private,
                        },
                    )
                    self.assertEqual(97, result.returncode, msg=result.stderr)
                    self.assertEqual(b"", result.stdout)
                    self.assertNotIn(private.encode(), result.stderr)
                    self.assertIn(b"unexpected fake docker invocation", result.stderr)
            self.assertEqual(list(requests), [call["arguments"] for call in fixture.calls()])

    def test_supported_projection_preserves_original_failure_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            identity = "sha256:" + "b" * 64
            label = '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}'
            forms = (
                ["image", "inspect", identity],
                ["image", "inspect", "--format", "{{.Id}}", identity],
                ["image", "inspect", "--format", label, identity],
                ["image", "inspect", "--format", "{{.Id}}|" + label, identity],
            )
            private = "private-supported-inspection-canary"
            for arguments in forms:
                for status in (7, 124, 137, 130, 143):
                    with self.subTest(arguments=arguments, status=status):
                        result = self.inspect(
                            fixture,
                            arguments,
                            {
                                "NMRNET_FAKE_INSPECT_STATUS": str(status),
                                "NMRNET_FAKE_INSPECT_PRIVATE_ERROR": private,
                            },
                        )
                        self.assertEqual(status, result.returncode)
                        self.assertEqual(b"", result.stdout)
                        self.assertEqual((private + "\n").encode(), result.stderr)
