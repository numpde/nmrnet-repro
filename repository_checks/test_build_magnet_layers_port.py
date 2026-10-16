"""Port reusable-image, offline install and native authority groups from Magnet.

https://github.com/numpde/magnet/blob/81db52dd6d2edc3a7a426cf9d71b505804342ad1/tests/repo_checks/test_dependency_layers.py
https://github.com/numpde/magnet/blob/81db52dd6d2edc3a7a426cf9d71b505804342ad1/tests/repo_checks/test_docker_build_contexts.py
https://github.com/numpde/magnet/blob/81db52dd6d2edc3a7a426cf9d71b505804342ad1/tests/repo_checks/check_rendered_compose.py

Layer PARENTS manifests, delta locks and source-specific Compose projects,
networks, volumes and datasets have no consumer here. Final-stage USER, verified
offline wheelhouse installation, no wheelhouse-triggered lock regeneration,
exact context rules and corresponding native namespace/mount/device/pull/OOM/
shm/restart/build grants retain their original assertion groups. Named local
wheelhouse build context is the existing verified NMRNet input exception.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import tempfile
import unittest
from pathlib import Path

from repository_checks.fixtures.containers.fixture import ROOT, LauncherFixture
from repository_checks.test_build_inputs_port import instructions

EXPECTED_RULES = {
    "containers/packages/Dockerfile.lock.dockerignore": """**
!tools/
!tools/container_guard.py
!tools/package_lock.py
!containers/
!containers/packages/
!containers/packages/Dockerfile.lock
!containers/packages/Dockerfile.lock.dockerignore
""",
    "containers/inference/Dockerfile.dockerignore": """**
!containers/
!containers/inference/
!containers/inference/Dockerfile
!containers/inference/Dockerfile.dockerignore
!tools/
!tools/container_guard.py
!tools/artifacts.py
!tools/wheelhouse.py
!tools/build_unicore.py
!requirements/
!requirements/inference.lock
!requirements/unicore-source.json
!NMRNetplusplus/
!NMRNetplusplus/LICENSE
!NMRNetplusplus/uninmr/
!NMRNetplusplus/uninmr/**/
!NMRNetplusplus/uninmr/**/*.py
""",
}


def final_stage_users(source):
    users = []
    for instruction in instructions(source):
        if instruction.startswith("FROM "):
            users = []
        elif instruction.startswith("USER "):
            users.append(instruction)
    return users


def options(arguments):
    result = {}
    for index, argument in enumerate(arguments):
        if argument.startswith("--"):
            key, separator, value = argument.partition("=")
            if not separator:
                value = arguments[index + 1] if index + 1 < len(arguments) else ""
            result.setdefault(key, []).append(value)
    return result


def byte_size(value):
    factor = {"k": 1024, "m": 1024**2, "g": 1024**3}.get(value[-1].lower(), 1)
    return int(value[:-1] if factor != 1 else value) * factor


# silent-defaults: allow-contract -- The checker profile admits network none, tmpfs up to 256 MiB and memory up to 2 GiB; reviewed lanes override resource bounds; proof=repository_checks/test_build_magnet_layers_port.py::NativeRenderedAuthorityChecks.test_native_grant_detector_rejects_each_extra_authority_group
def assert_runtime_grants(case, arguments, *, tmpfs_max=256 * 1024**2, memory_max=2 * 1024**3, network="none"):
    values = options(arguments)
    forbidden = {
        "--privileged",
        "--device",
        "--device-cgroup-rule",
        "--gpus",
        "--runtime",
        "--dns",
        "--dns-option",
        "--dns-search",
        "--add-host",
        "--link",
        "--group-add",
        "--oom-kill-disable",
        "--publish",
        "--publish-all",
        "--sysctl",
        "--ulimit",
        "--volumes-from",
        "--cap-add",
        "--cgroup-parent",
        "--volume",
    }
    case.assertFalse(forbidden & values.keys(), msg="Native runtime acquired forbidden authority.")
    for namespace in ("--cgroupns", "--ipc", "--pid", "--uts"):
        # silent-defaults: allow-contract -- Omission requests no explicit namespace override; every supplied override must be private; proof=repository_checks/test_build_magnet_layers_port.py::NativeRenderedAuthorityChecks.test_native_grant_detector_rejects_each_extra_authority_group
        case.assertTrue(all(value == "private" for value in values.get(namespace, [])))
    # silent-defaults: allow-contract -- An absent user-namespace option requests no override; an explicit host namespace is rejected; proof=repository_checks/test_build_magnet_layers_port.py::NativeRenderedAuthorityChecks.test_native_grant_detector_rejects_each_extra_authority_group
    case.assertNotIn("host", values.get("--userns", []))
    # Absence leaves image acquisition to the engine's pull policy.
    case.assertIn("--pull", values, msg="Native runtime must explicitly forbid image pulls.")
    case.assertTrue(all(value == "never" for value in values["--pull"]))
    # silent-defaults: allow-contract -- Omission has no explicit restart request to inspect; supplied restart policies must be no; proof=repository_checks/test_build_magnet_layers_port.py::NativeRenderedAuthorityChecks.test_native_grant_detector_rejects_each_extra_authority_group
    case.assertTrue(all(value == "no" for value in values.get("--restart", [])))
    # silent-defaults: allow-contract -- Omission has no explicit OOM-score adjustment to inspect; supplied adjustments must be nonnegative; proof=repository_checks/test_build_magnet_layers_port.py::NativeRenderedAuthorityChecks.test_native_grant_detector_rejects_each_extra_authority_group
    case.assertTrue(all(int(value) >= 0 for value in values.get("--oom-score-adj", [])))
    # silent-defaults: allow-contract -- Omission has no explicit shared-memory size to inspect; supplied requests must not exceed 64 MiB; proof=repository_checks/test_build_magnet_layers_port.py::NativeRenderedAuthorityChecks.test_native_grant_detector_rejects_each_extra_authority_group
    case.assertTrue(all(byte_size(value) <= 64 * 1024**2 for value in values.get("--shm-size", [])))
    case.assertIn("--read-only", values)
    case.assertEqual(["ALL"], values.get("--cap-drop"))
    case.assertEqual(["no-new-privileges:true"], values.get("--security-opt"))
    case.assertTrue(0 < int(values["--pids-limit"][0]) <= 128)
    case.assertTrue(0 < float(values["--cpus"][0]) <= 2)
    case.assertTrue(0 < byte_size(values["--memory"][0]) <= memory_max)
    case.assertEqual(values["--memory-swap"], values["--memory"])
    case.assertEqual([network], values["--network"])
    # silent-defaults: allow-contract -- Omission supplies no explicit bind mounts to inspect; every supplied mount must pass the authority checks below; proof=repository_checks/test_build_magnet_layers_port.py::NativeRenderedAuthorityChecks.test_native_grant_detector_rejects_each_extra_authority_group
    for mount in values.get("--mount", []):
        case.assertTrue(mount.startswith("type=bind,"))
        case.assertNotIn("bind-propagation=", mount)
        case.assertNotIn("bind-recursive=writable", mount)
        case.assertNotIn("selinux=", mount)
        case.assertNotIn("docker.sock", mount)
    case.assertEqual(1, len(values.get("--tmpfs", [])))
    for tmpfs in values["--tmpfs"]:
        target, settings = tmpfs.split(":", 1)
        flags = set(settings.split(","))
        case.assertTrue({"rw", "nosuid", "nodev", "noexec"} <= flags)
        case.assertFalse({"exec", "suid", "dev"} & flags)
        case.assertEqual("/tmp", target)
        sizes = [setting.split("=", 1)[1] for setting in flags if setting.startswith("size=")]
        case.assertEqual(1, len(sizes), msg="Writable tmpfs has no single finite owned size.")
        case.assertTrue(0 < byte_size(sizes[0]) <= tmpfs_max)


class DependencyLayerChecks(unittest.TestCase):
    def test_reusable_images_default_to_nonroot_in_the_final_stage(self):
        for relative in ("containers/inference/Dockerfile", "containers/packages/Dockerfile.lock"):
            with self.subTest(relative=relative):
                users = final_stage_users((ROOT / relative).read_text())
                self.assertTrue(users, msg="Final image stage has no owned USER instruction.")
                self.assertEqual("USER 65532:65532", users[-1])

    def test_final_stage_identity_does_not_inherit_an_earlier_stage_user(self):
        self.assertEqual([], final_stage_users("FROM python AS earlier\nUSER 65532:65532\nFROM python\n"))
        self.assertEqual(
            ["USER 65532:65532"], final_stage_users("FROM python AS earlier\nUSER root\nFROM python\nUSER 65532:65532\n")
        )

    def test_image_installs_verified_wheelhouse_without_removing_owned_dependencies(self):
        source = (ROOT / "containers/inference/Dockerfile").read_text()
        phases = instructions(source)
        install = next(line for line in phases if "pip install" in line)
        self.assertIn("python3 -B -m tools.wheelhouse check", install)
        self.assertLess(install.index("tools.wheelhouse check"), install.index("pip install"))
        for flag in ("--no-index", "--require-hashes", "--only-binary=:all:", "--find-links /wheelhouse"):
            self.assertIn(flag, install)
        self.assertNotIn("pip sync", install)
        self.assertIn("python3 -m pip check", install)
        self.assertIn("--mount=type=bind,from=wheelhouse,target=/wheelhouse", install)
        # The existing NMRNet verifier compares .complete to a full hash/size
        # inventory rather than Magnet's sha256sum file. Its unit tests exercise
        # corrupted receipts/content; this assertion only proves build ordering.
        verifier = (ROOT / "tools/wheelhouse.py").read_text()
        self.assertIn('read_json(output / ".complete")', verifier)
        self.assertIn("receipt != inventory(lock, source, output)", verifier)

    def test_wheelhouse_target_does_not_depend_on_lock_generation(self):
        source = (ROOT / "Makefile").read_text()
        target = source.split("\npackages/wheelhouse:\n", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn("packages/lock/", target)
        self.assertIn("tools/wheelhouse.sh download", target)
        launcher = (ROOT / "tools/wheelhouse.sh").read_text()
        for forbidden in ("packages.sh", "package_lock", "uv pip compile"):
            self.assertNotIn(forbidden, launcher)


class DockerBuildContextChecks(unittest.TestCase):
    def test_build_contexts_expose_only_reviewed_inputs(self):
        for relative, expected in EXPECTED_RULES.items():
            with self.subTest(relative=relative):
                self.assertEqual(tuple(expected.splitlines()), tuple((ROOT / relative).read_text().splitlines()))

    def test_added_non_tool_secret_leaf_breaks_the_reviewed_context_tuple(self):
        for expected in EXPECTED_RULES.values():
            self.assertNotEqual(tuple(expected.splitlines()), tuple((expected + "!requirements/private.json\n").splitlines()))


class NativeRenderedAuthorityChecks(unittest.TestCase):
    def test_offline_native_runtime_grants_preserve_all_corresponding_compose_groups(self):
        # Numeric profiles follow each reviewed target's actual workload;
        # the original resource and swap bounds remain assertions, while
        # a scientific run is not assigned a different lane's 2-GiB budget.
        lanes = (
            ("container.sh", "test", {}, 64 * 1024**2, 512 * 1024**2),
            ("container.sh", "test/repository", {}, 64 * 1024**2, 512 * 1024**2),
            ("inference_image.sh", "import", {}, 256 * 1024**2, 2 * 1024**3),
            ("packages.sh", "apply", {"NMRNET_STAGE_INPUT": "lock.fixture"}, 1024**3, 2 * 1024**3),
            ("wheelhouse.sh", "check", {}, 64 * 1024**2, 512 * 1024**2),
            ("baseline.sh", "prepare", {}, 256 * 1024**2, 2 * 1024**3),
            ("baseline.sh", "run", {}, 256 * 1024**2, 4 * 1024**3),
        )
        for launcher, action, lane_additions, tmpfs_max, memory_max in lanes:
            with self.subTest(launcher=launcher, action=action), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                baseline = fixture.baseline_inputs()
                stage = fixture.root / ".work/packages-stage/lock.fixture"
                stage.mkdir(parents=True)
                (stage / "image-id").write_text("sha256:" + "b" * 64 + "\n")
                additions = baseline if launcher == "baseline.sh" and action == "run" else lane_additions
                before = len(fixture.calls())
                prepared = fixture.root / ".work/baseline/prepared"
                saved = fixture.root / ".work/baseline/prepared.prerequisite"
                if launcher == "baseline.sh" and action == "prepare":
                    # This target creates prepared output; the later run
                    # needs the separately seeded prepared prerequisite.
                    prepared.rename(saved)
                try:
                    result = fixture.run(launcher, action, additions=additions)
                finally:
                    if saved.exists():
                        saved.rename(prepared)
                self.assertEqual(0, result.returncode, msg=result.stderr)
                calls = fixture.calls()[before:]
                runs = [call["arguments"] for call in calls if call["arguments"][:1] == ["run"]]
                self.assertEqual(1, len(runs))
                assert_runtime_grants(self, runs[0], tmpfs_max=tmpfs_max, memory_max=memory_max)

    def test_native_grant_detector_rejects_each_extra_authority_group(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            result = fixture.run("inference_image.sh", "import")
            self.assertEqual(0, result.returncode, msg=result.stderr)
            arguments = next(call["arguments"] for call in fixture.calls() if call["arguments"][:1] == ["run"])
            assert_runtime_grants(self, arguments)
            pull_index = arguments.index("--pull")
            self.assertEqual("never", arguments[pull_index + 1])
            with self.subTest(authority="implicit image pull"), self.assertRaises(AssertionError):
                assert_runtime_grants(self, [*arguments[:pull_index], *arguments[pull_index + 2 :]])
            for additions in (
                ["--pid=host"],
                ["--ipc=host"],
                ["--cgroupns=host"],
                ["--userns=host"],
                ["--device=/dev/null"],
                ["--gpus=all"],
                ["--sysctl=kernel.foo=1"],
                ["--oom-kill-disable"],
                ["--oom-score-adj=-1"],
                ["--shm-size=65m"],
                ["--restart=always"],
                ["--pull=always"],
                ["--mount=type=bind,src=/input,dst=/input,bind-propagation=shared"],
                ["--cap-add=SYS_ADMIN"],
            ):
                with self.subTest(additions=additions), self.assertRaises(AssertionError):
                    assert_runtime_grants(self, arguments + additions)

    def test_missing_oversized_or_extra_writable_tmpfs_breaks_owned_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            result = fixture.run("inference_image.sh", "import")
            self.assertEqual(0, result.returncode, msg=result.stderr)
            arguments = next(call["arguments"] for call in fixture.calls() if call["arguments"][:1] == ["run"])
            index = arguments.index("--tmpfs") + 1
            for value in ("/tmp:rw,nosuid,nodev,noexec", "/tmp:rw,nosuid,nodev,noexec,size=257m"):
                with self.subTest(value=value), self.assertRaises(AssertionError):
                    assert_runtime_grants(self, [*arguments[:index], value, *arguments[index + 1 :]])
            with self.assertRaises(AssertionError):
                assert_runtime_grants(self, [*arguments, "--tmpfs", "/extra:rw,nosuid,nodev,noexec,size=1m"])

    def test_native_build_has_no_extra_grants_and_dockerfile_is_inside_real_context(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            result = fixture.run("inference_image.sh", "build")
            self.assertEqual(0, result.returncode, msg=result.stderr)
            arguments = next(call["arguments"] for call in fixture.calls() if call["arguments"][:1] == ["build"])
            values = options(arguments)
            self.assertEqual(["none"], values["--network"])
            self.assertEqual(["false"], values["--pull"])
            forbidden = {
                "--cache-from",
                "--cache-to",
                "--allow",
                "--add-host",
                "--privileged",
                "--output",
                "--secret",
                "--shm-size",
                "--ssh",
                "--tag",
                "--ulimit",
            }
            self.assertFalse(forbidden & values.keys())
            self.assertTrue(Path(values["--file"][0]).is_relative_to(Path(arguments[-1])))
            capture = Path(arguments[-1]).parent
            self.assertEqual(fixture.root / ".work/inference", capture.parent)
            self.assertTrue(capture.name.startswith(".build."))
            self.assertEqual(["wheelhouse=" + str(capture / "wheelhouse")], values["--build-context"])
            self.assertTrue(fixture.build_evidence()["manifest"])
            self.assertEqual(
                {".complete": {"sha256": hashlib.sha256(b"{}\n").hexdigest(), "mode": 0o644}},
                fixture.build_evidence()["wheelhouse_manifest"],
            )
            self.assertFalse(capture.exists(), msg="The completed builder retained its disposable inputs")


if __name__ == "__main__":
    unittest.main()
