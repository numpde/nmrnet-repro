"""Exercise the maintained launcher with an empty contained proof directory.

Port: https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_unittest_runner.py#L10
Magnet's custom runner rejects zero tests with status 2. Our pinned Python
3.12.12 unittest runner owns status 5 and the NO TESTS RAN diagnostic. Preserve
the refusal to report an empty suite as success, rather than invent a wrapper
merely to copy Magnet's numeric status. The fake engine executes the selected
Python argv or exact image Bash wrapper without a socket or mounts; its fixed
container HOME is mapped to private fixture storage, and only discovery input
is replaced by the empty proof directory.
"""

from tools.container_guard import require_container

require_container()

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from repository_checks.fixtures.containers.fixture import LauncherFixture
from repository_checks.quality.sources import ROOT

FIXTURES = ROOT / "repository_checks/fixtures/containers"


class UnittestRunnerTests(unittest.TestCase):
    def test_projection_git_adapter_rejects_unknown_metadata_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            prefix = [
                str(fixture.root / "bin/git"),
                "--git-dir=" + str(fixture.root / ".git"),
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
            ]
            positive = subprocess.run(
                [*prefix, "rev-parse", "--verify", "HEAD^{commit}"], env=fixture.environment, capture_output=True, check=False
            )
            self.assertEqual(0, positive.returncode, msg=positive.stderr)
            self.assertEqual(("a" * 40 + "\n").encode(), positive.stdout)
            requests = [
                [*prefix, "cat-file", "blob", "a" * 40],
                [*prefix, "rev-parse", "--verify HEAD^{commit}"],
                [*prefix, "ls-tree", "b" * 40, "--", "NMRNetplusplus"],
                [
                    prefix[0],
                    "--git-dir=" + str(fixture.root / ".git/unknown"),
                    *prefix[2:],
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                ],
                [*prefix[:5], "core.fsmonitor=true", "rev-parse", "--verify", "HEAD^{commit}"],
            ]
            for command in requests:
                with self.subTest(command=command):
                    result = subprocess.run(command, env=fixture.environment, capture_output=True, check=False)
                    self.assertEqual(97, result.returncode, msg=result.stderr)
                    self.assertEqual(b"", result.stdout)

    def test_empty_discovery_adapter_rejects_other_shell_or_python_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = root / "empty"
            empty.mkdir()
            environment = os.environ | {
                "PYTHONPATH": str(ROOT),
                "NMRNET_FAKE_ENGINE_LOG": str(root / "calls"),
                "NMRNET_FAKE_EMPTY_DISCOVERY": "1",
                "NMRNET_FAKE_EMPTY_DIRECTORY": str(empty),
            }
            commands = [
                ["bash", "-eu", "-c", "printf 'unmodeled child ran'"],
                ["python3", "-B", "-m", "unittest", "discover", "-s", "other-tests", "-v"],
                ["bash", "-eu", "-c", "printf unmodeled", "python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"],
                ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-v", ";", "printf", "unmodeled"],
            ]
            for command in commands:
                with self.subTest(command=command):
                    result = subprocess.run(
                        [str(FIXTURES / "docker.py"), "run", "sha256:" + "b" * 64, *command],
                        env=environment,
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(97, result.returncode, msg=result.stderr)
                    self.assertEqual("", result.stdout)
                    self.assertIn("unexpected fake docker invocation:", result.stderr)
            for prefix in (["run", "--unknown-flag", "sha256:" + "b" * 64], ["run", "unmodeled-image:latest"]):
                with self.subTest(prefix=prefix):
                    result = subprocess.run(
                        [
                            str(FIXTURES / "docker.py"),
                            *prefix,
                            "python3",
                            "-B",
                            "-m",
                            "unittest",
                            "discover",
                            "-s",
                            "tests",
                            "-v",
                        ],
                        env=environment,
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(97, result.returncode, msg=result.stderr)
                    self.assertEqual("", result.stdout)
                    self.assertIn("unexpected fake docker invocation:", result.stderr)

    def test_empty_discovery_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = LauncherFixture(root)
            empty = root / "empty"
            empty.mkdir()
            additions = {"NMRNET_FAKE_EMPTY_DISCOVERY": "1", "NMRNET_FAKE_EMPTY_DIRECTORY": str(empty)}
            for lane in ("test", "test/repository"):
                with self.subTest(lane=lane):
                    before = len(fixture.calls())
                    result = fixture.run("container.sh", lane, additions=additions)
                    self.assertEqual(5, result.returncode, msg="An empty maintained proof lane must fail: " + result.stderr)
                    self.assertIn("NO TESTS RAN", result.stderr)
                    runs = [call["arguments"] for call in fixture.calls()[before:] if call["arguments"][:1] == ["run"]]
                    self.assertEqual(1, len(runs), msg="Discovery did not reach its selected contained run")
                    record = json.loads((root / "discovery-evidence.jsonl").read_text().splitlines()[-1])
                    self.assertEqual(5, record["status"])
                    if lane == "test":
                        self.assertEqual("tests", record["original"][6])
                        self.assertEqual(str(empty), record["mapped"][6])
                    else:
                        self.assertEqual(["bash", "-eu", "-c"], record["original"][:3])
                        self.assertIn("discover -s repository_checks -v", record["original"][3])
                        self.assertIn("discover -s " + str(empty) + " -v", record["mapped"][3])
                        self.assertEqual(0o700, record["home_mode"])
                        self.assertEqual(
                            "[safe]\ndirectory = /workspace\ndirectory = /workspace/NMRNetplusplus\n", record["home_config"]
                        )
                        self.assertFalse(Path(record["home"]).exists())
                        mounts = [runs[0][index + 1] for index, value in enumerate(runs[0]) if value == "--mount"]
                        git_mounts = [mount for mount in mounts if ",dst=/workspace/.git" in mount]
                        self.assertEqual(3, len(git_mounts))
                        self.assertTrue(all(mount.endswith(",readonly") for mount in git_mounts))
                        self.assertFalse(list(root.glob(".work/repository-check-inputs.*")))

    def test_fake_docker_rejects_unmodeled_invocations(self):
        with tempfile.TemporaryDirectory() as directory:
            for arguments in (("unexpected-command", "private-sentinel"), ("compose", "frobnicate", "private-sentinel")):
                with self.subTest(arguments=arguments):
                    result = subprocess.run(
                        [str(FIXTURES / "docker.py"), *arguments],
                        env=os.environ
                        | {
                            "PYTHONPATH": str(ROOT),
                            "PATH": str(Path(sys.executable).parent) + ":" + os.defpath,
                            "NMRNET_FAKE_ENGINE_LOG": str(Path(directory) / "calls"),
                        },
                        text=True,
                        capture_output=True,
                        timeout=10,
                        check=False,
                    )
                    self.assertEqual(97, result.returncode)
                    self.assertIn("unexpected fake docker invocation:", result.stderr)
                    self.assertIn("private-sentinel", result.stderr)


if __name__ == "__main__":
    unittest.main()
