"""Bind provenance reads to their selected checkout using actual offline Git."""

from tools.container_guard import require_container

require_container()

import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import test_baseline_launcher as baseline_fixture
from tests import test_inference_image as image_fixture

from repository_checks.quality.sources import ROOT


class GitMetadataTests(unittest.TestCase):
    def git(self, repository, *arguments):
        environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        environment |= {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Finite metadata fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Finite metadata fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
        }
        result = subprocess.run(
            [
                "/usr/bin/git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(repository),
                *arguments,
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return result.stdout.strip()

    def test_baseline_reads_selected_head_and_index_under_ambient_poison(self):
        fixture = baseline_fixture.BaselineRunLauncherTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.git(fixture.root, "init", "--initial-branch=main")
        self.git(fixture.root, "add", "--all")
        self.git(fixture.root, "update-index", "--add", "--cacheinfo", "160000," + "b" * 40 + ",NMRNetplusplus")
        self.git(fixture.root, "commit", "-m", "Selected provenance")
        selected = self.git(fixture.root, "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as directory:
            foreign = Path(directory)
            self.git(foreign, "init", "--initial-branch=main")
            (foreign / "unrelated").write_text("Other repository authority\n")
            self.git(foreign, "add", "--all")
            self.git(foreign, "update-index", "--add", "--cacheinfo", "160000," + "c" * 40 + ",NMRNetplusplus")
            self.git(foreign, "commit", "-m", "Foreign provenance")
            self.assertNotEqual(selected, self.git(foreign, "rev-parse", "HEAD"))
            environment = fixture.environment | {"PATH": os.environ["PATH"]}
            # silent-defaults: allow-pop-default -- Remove any inherited exported Git function before observing provenance through the real executable; proof=repository_checks/test_git_metadata.py::GitMetadataTests.test_baseline_reads_selected_head_and_index_under_ambient_poison
            environment.pop("BASH_FUNC_git%%", None)
            record = fixture.root / ".work/inference/image-id"
            before = record.read_bytes(), record.stat().st_ino
            outputs = []
            for poison in (
                {},
                {"GIT_DIR": str(foreign / ".git")},
                {"GIT_INDEX_FILE": str(foreign / ".git/index")},
                {"GIT_CONFIG_COUNT": "not-a-count"},
                {"GIT_DIR": str(foreign / ".git"), "GIT_WORK_TREE": str(foreign)},
            ):
                with self.subTest(poison=poison):
                    # The final foreign index matches the real upstream label;
                    # label admission alone cannot detect a substituted HEAD.
                    if "GIT_WORK_TREE" in poison:
                        self.git(foreign, "update-index", "--cacheinfo", "160000," + "b" * 40 + ",NMRNetplusplus")
                    result = subprocess.run(
                        ["bash", str(fixture.root / "tools/baseline.sh"), "--print", "run"],
                        env=environment | poison,
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=False,
                    )
                    self.assertEqual(0, result.returncode, msg=result.stderr)
                    self.assertEqual("", result.stderr)
                    arguments = baseline_fixture.decode_command(result.stdout)
                    self.assertEqual(selected, arguments[arguments.index("--repository-revision") + 1])
                    self.assertEqual("b" * 40, arguments[arguments.index("--upstream-revision") + 1])
                    self.assertEqual(before, (record.read_bytes(), record.stat().st_ino))
                    self.assertFalse((fixture.root / ".work/baseline/runs").exists())
                    outputs.append(result.stdout)
            self.assertEqual(5, len(outputs))
            self.assertEqual([outputs[0]] * len(outputs), outputs)

    def test_inference_print_reads_selected_parent_and_module_under_poison(self):
        fixture = image_fixture.ImageLauncherTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        module = fixture.root / "NMRNetplusplus"
        (module / "uninmr").mkdir(parents=True)
        (module / "LICENSE").write_text("Finite upstream license\n")
        (module / "uninmr/infer.py").write_text("finite_source = 1\n")
        self.git(module, "init", "--initial-branch=main")
        self.git(module, "add", "--all")
        self.git(module, "commit", "-m", "Selected source")
        revision = self.git(module, "rev-parse", "HEAD")
        self.git(fixture.root, "init", "--initial-branch=main")
        self.git(fixture.root, "add", "--all")
        self.git(fixture.root, "update-index", "--add", "--cacheinfo", "160000," + revision + ",NMRNetplusplus")
        self.git(fixture.root, "commit", "-m", "Selected parent")
        parent = self.git(fixture.root, "rev-parse", "HEAD")
        self.assertNotEqual(parent, revision)
        # Foreign metadata must not replace the selected parent index or the
        # selected module's HEAD/status, including an unrelated staged gitlink.
        poisons = (
            {},
            {"GIT_DIR": str(fixture.root / ".git")},
            {"GIT_INDEX_FILE": str(module / ".git/index")},
            {"GIT_CONFIG_COUNT": "not-a-count"},
            {"GIT_DIR": str(module / ".git"), "GIT_WORK_TREE": str(module)},
        )
        outputs = []
        for poison in poisons:
            with self.subTest(poison=poison):
                # The selected index remains authoritative; only foreign
                # metadata is poisoned. Use the module's index for the DIR
                # override after giving it an unrelated same-name gitlink.
                if "GIT_WORK_TREE" in poison:
                    self.git(module, "update-index", "--add", "--cacheinfo", "160000," + parent + ",NMRNetplusplus")
                result = subprocess.run(
                    ["bash", str(fixture.root / "tools/inference_image.sh"), "--print", "build"],
                    env=os.environ | poison,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(0, result.returncode, msg=result.stderr)
                self.assertEqual("", result.stderr)
                arguments = shlex.split(result.stdout.splitlines()[-1])
                self.assertIn("NMRNET_REVISION=" + revision, arguments)
                self.assertFalse((fixture.root / ".work/inference").exists())
                outputs.append(result.stdout)
        self.assertEqual(5, len(outputs))
        self.assertEqual([outputs[0]] * len(outputs), outputs)

    def test_binary_lookup_failure_and_exported_function_keep_distinct_status(self):
        for environment, status, diagnostic in (
            (os.environ | {"PATH": "/nonexistent"}, 1, ""),
            (
                os.environ | {"BASH_FUNC_git%%": "() { :; }"},
                2,
                "Cannot read repository Git metadata: Git must resolve to an executable file.\n",
            ),
        ):
            with self.subTest(status=status):
                result = subprocess.run(
                    ["/bin/bash", "-euc", 'source "$1"; repository_git --version', "bash", str(ROOT / "tools/git_metadata.sh")],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(status, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertEqual(diagnostic, result.stderr)

    def test_unit_git_double_accepts_only_exact_controls_queries_and_states(self):
        fixture = baseline_fixture.BaselineRunLauncherTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        executable = str(ROOT / "tests/fixtures/git_metadata.sh")
        controls = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-C"]
        root = str(fixture.root)
        requests = (
            ([*controls, root, "rev-parse", "--verify", "HEAD"], "d" * 40 + "\n"),
            ([*controls, root, "ls-files", "--stage", "--", "NMRNetplusplus"], "160000 " + "b" * 40 + " 0 NMRNetplusplus\n"),
            ([*controls, root + "/NMRNetplusplus", "rev-parse", "HEAD"], "b" * 40 + "\n"),
            (
                [
                    *controls,
                    root + "/NMRNetplusplus",
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--",
                    "uninmr",
                    "LICENSE",
                ],
                "",
            ),
            ([*controls, root + "/NMRNetplusplus", "ls-files", "--others", "--", "uninmr", "LICENSE"], ""),
        )
        for arguments, expected in requests:
            result = subprocess.run(
                [executable, *arguments], env=os.environ | fixture.git_environment, capture_output=True, text=True, check=False
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual(expected, result.stdout)
            self.assertEqual("", result.stderr)
        valid = requests[0][0]
        invalid = (
            [*valid, ""],
            valid[:-1],
            [*controls, root, "rev-parse --verify", "HEAD"],
            [*controls, root, "rev-parse", "--verify HEAD", ""],
            [*controls, root, "ls-files", "--stage", "--", "NMRNetplusplus", ""],
            [*controls, root + "/other", "rev-parse", "--verify", "HEAD"],
            [*controls, root + "/NMRNetplusplus", "rev-parse", "--verify", "HEAD"],
            [*controls, root, "rev-parse", "HEAD"],
            ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=true", *valid[4:]],
            valid[4:],
        )
        for arguments in invalid:
            result = subprocess.run(
                [executable, *arguments], env=os.environ | fixture.git_environment, capture_output=True, text=True, check=False
            )
            self.assertEqual(97, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertEqual("", result.stderr)
        for state in ("dirty", "failure", "unknown"):
            result = subprocess.run(
                [executable, *requests[3][0]],
                env=os.environ | fixture.git_environment | {"NMRNET_TEST_GIT_STATE": state},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual({"dirty": 0, "failure": 1, "unknown": 97}[state], result.returncode)
            self.assertEqual(" M uninmr/infer.py\n" if state == "dirty" else "", result.stdout)
            self.assertEqual("", result.stderr)
