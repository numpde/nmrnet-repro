"""Prove the builder consumes captured committed inputs and verified wheel bytes."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit.wheel_pool_fixtures import locked
from tests.unit.wheel_pool_fixtures import wheel as fixture_wheel

from repository_checks.fixtures.containers.fixture import LauncherFixture


class CapturedInputTests(unittest.TestCase):
    def test_installer_is_captured_and_precedes_scientific_installation(self):
        root = Path(__file__).resolve().parents[1]
        prefix = "host/bootstrap/acquisition/acquisition_tool/installer/"
        selectors = set((root / "containers/inference/image-inputs.txt").read_text().splitlines())
        inputs = {
            prefix + "requirements/installer-release.json",
            prefix + "tools/container_guard.py",
            prefix + "tools/installer.py",
            prefix + "wheels/pip-26.2.2-py3-none-any.whl",
        }
        self.assertTrue(inputs <= selectors)
        rules = set((root / "containers/inference/Dockerfile.dockerignore").read_text().splitlines())
        self.assertTrue({"!" + path for path in inputs} <= rules)
        dockerfile = (root / "containers/inference/Dockerfile").read_text()
        bootstrap = dockerfile.index("python3 -B -m tools.installer")
        self.assertLess(bootstrap, dockerfile.index("python3 -m pip install"))
        self.assertLess(bootstrap, dockerfile.index("python3 -B -m tools.provider_environment"))
        self.assertIn("--python /usr/local/bin/python", dockerfile)

    def author_pool(self, fixture, pool):
        wheel = fixture_wheel(pool, "fixture", "1")
        locked(fixture.root / "requirements/inference.lock", [("fixture", "1", [wheel])])
        for name in ("provider-core-native-install.lock",):
            locked(fixture.root / "requirements" / name, [("fixture", "1", [wheel])])
        archive = pool / ("Uni-Core-" + "a" * 40 + ".tar.gz")
        archive.write_bytes(b"synthetic source bytes")
        (fixture.root / "requirements/unicore-source.json").write_text(
            json.dumps(
                {
                    "name": "unicore",
                    "version": "0.0.1",
                    "commit": "a" * 40,
                    "url": "https://codeload.github.com/dptech-corp/Uni-Core/tar.gz/" + "a" * 40,
                    "size": archive.stat().st_size,
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                }
            )
        )
        fixture.capture_committed_inputs()
        return wheel

    def assert_retired_capture(self, fixture):
        self.assertEqual([], list((fixture.root / ".work/inference").glob(".build.*")))

    def test_git_double_requires_the_stored_commit_pathspec_controls_and_blob(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            paths = (fixture.root / ".fake-git/parent/selection.paths").read_text().splitlines()
            command = [str(fixture.root / "bin/git"), "-C", str(fixture.root)]
            accepted = ["ls-tree", "-r", "-z", "a" * 40, "--", *paths]
            positive = subprocess.run(command + accepted, env=fixture.environment, capture_output=True, check=False)
            self.assertEqual(0, positive.returncode, msg=positive.stderr)
            self.assertEqual((fixture.root / ".fake-git/parent/tree").read_bytes(), positive.stdout)
            object_id = positive.stdout.split(b"\0")[0].split(b"\t")[0].split()[-1].decode("ascii")
            requests = [
                ["ls-tree", "-r", "-z", "b" * 40, "--", *paths],
                ["ls-tree", "-r", "-z", "a" * 40, "--", "tools"],
                ["ls-tree", "-r", "-z", "a" * 40, "--", *paths[:-1]],
                ["ls-tree", "-r", "-z", "a" * 40, "--", *reversed(paths)],
                ["ls-tree", "--full-tree", "-r", "-z", "a" * 40, "--", *paths],
                ["ls-tree", "b" * 40, "--", "NMRNetplusplus"],
                ["cat-file", "blob", "f" * 40],
                ["cat-file", "blob", "a" * 40 + ":requirements/inference.lock"],
                ["rev-parse", "--verify", "HEAD", "extra"],
                ["ls-tree", "-r -z", "", "a" * 40, "--", *paths],
                ["ls-tree", "-r", "-z", "a" * 40, "--", paths[0] + "\n" + paths[1], *paths[2:]],
                ["cat-file blob", "", object_id],
                ["rev-parse", "--verify HEAD"],
                ["rev-parse", "--verify HEAD^{commit}"],
                ["ls-files", "--others", "--", "tools", ""],
            ]
            for request in requests:
                with self.subTest(request=request):
                    result = subprocess.run(command + request, env=fixture.environment, capture_output=True, check=False)
                    self.assertEqual(97, result.returncode, msg=result.stderr)
                    self.assertEqual(b"", result.stdout)
            module = [command[0], "-C", str(fixture.root / "NMRNetplusplus")]
            requests = [
                ["status", "--porcelain", "--untracked-files=all", "--", "LICENSE uninmr"],
                ["status", "--porcelain", "--untracked-files=all", "--", "LICENSE uninmr", ""],
                ["status", "--porcelain", "--untracked-files=all", "--", "LICENSE", "uninmr", ""],
                ["ls-files", "--others", "--", "LICENSE uninmr"],
                ["ls-files", "--others", "--", "LICENSE uninmr", ""],
                ["ls-files", "--others", "--", "LICENSE", "uninmr", ""],
            ]
            for request in requests:
                with self.subTest(module_request=request):
                    result = subprocess.run(module + request, env=fixture.environment, capture_output=True, check=False)
                    self.assertEqual(97, result.returncode, msg=result.stderr)
                    self.assertEqual(b"", result.stdout)
            result = subprocess.run(
                [command[0], "-c", "core.fsmonitor=unexpected", *command[1:], *accepted],
                env=fixture.environment,
                capture_output=True,
                check=False,
            )
            self.assertEqual(97, result.returncode, msg=result.stderr)
            self.assertEqual(b"", result.stdout)

    def test_stored_malformed_gitlink_and_changed_upstream_head_never_reach_engine(self):
        for mutation in ("mode", "revision", "head"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                parent = fixture.root / ".fake-git/parent"
                if mutation == "mode":
                    (parent / "gitlink").write_text("100644 blob " + "a" * 40 + "\tNMRNetplusplus\n")
                elif mutation == "revision":
                    (parent / "gitlink").write_text("160000 commit " + "b" * 40 + "\tNMRNetplusplus\n")
                else:
                    (fixture.root / ".fake-git/module/HEAD").write_text("b" * 40 + "\n")
                result = fixture.run("inference_image.sh", "build")
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertEqual([], fixture.calls())
                self.assert_retired_capture(fixture)

    def test_uninitialized_upstream_submodule_names_repair_before_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            (fixture.root / "NMRNetplusplus/.git").unlink()
            result = fixture.run("inference_image.sh", "build")
            self.assertEqual(2, result.returncode, msg=result.stderr)
            self.assertIn("upstream source submodule is not initialized", result.stderr)
            self.assertIn("git submodule update --init --recursive", result.stderr)
            self.assertEqual([], fixture.calls())
            self.assert_retired_capture(fixture)

    def test_build_consumes_committed_bytes_and_modes_after_worktree_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            source = fixture.root / "tools/wheelhouse.py"
            original = hashlib.sha256(source.read_bytes()).hexdigest()
            result = fixture.run("inference_image.sh", "build", additions={"NMRNET_FAKE_MUTATE_INPUT": str(source)})
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual({"sha256": original, "mode": 0o644}, fixture.build_evidence()["manifest"]["tools/wheelhouse.py"])
            self.assertEqual(0o700, fixture.build_evidence()["capture_mode"])
            self.assertEqual(os.getuid(), fixture.build_evidence()["capture_uid"])
            # Git does not own directory modes. Source copied into the nonroot
            # image must stay discoverable despite the capture owner's umask077.
            # Include intermediate parents: mkdir -p -m755 alone misses them.
            for path in (
                "artifacts",
                "containers",
                "containers/inference",
                "requirements",
                "tools",
                "NMRNetplusplus/uninmr",
            ):
                with self.subTest(directory=path):
                    self.assertEqual(0o755, fixture.build_evidence()["directory_modes"][path])
            self.assertEqual(b"concurrent worktree mutation\n", source.read_bytes())
            self.assert_retired_capture(fixture)

    def test_dirty_authored_inputs_never_reach_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            result = fixture.run("inference_image.sh", "build", additions={"NMRNET_FAKE_DIRTY_ROOT": "1"})
            self.assertEqual(2, result.returncode, msg=result.stderr)
            self.assertEqual([], fixture.calls())
            self.assert_retired_capture(fixture)

    def test_live_bytes_modes_and_linked_ancestors_must_match_committed_inputs(self):
        for mutation in ("bytes", "mode", "ancestor"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                source = fixture.root / "tools/wheelhouse.py"
                if mutation == "bytes":
                    source.write_bytes(b"dirty\n")
                elif mutation == "mode":
                    source.chmod(0o755)
                else:
                    tools = fixture.root / "tools"
                    tools.rename(fixture.root / "outside-tools")
                    tools.symlink_to(fixture.root / "outside-tools", target_is_directory=True)
                result = fixture.run("inference_image.sh", "build")
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertEqual([], fixture.calls())
                self.assert_retired_capture(fixture)

    def test_manifest_mismatch_rejects_captured_source_before_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            manifest = fixture.root / "artifacts/nmrnet-source.sha256"
            manifest.write_text("0" * 64 + "  LICENSE\n")
            fixture.capture_committed_inputs()
            result = fixture.run("inference_image.sh", "build")
            self.assertEqual(2, result.returncode, msg=result.stderr)
            self.assertEqual([], fixture.calls())
            self.assert_retired_capture(fixture)

    def test_source_executable_mode_is_materialized_from_the_committed_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            source = fixture.root / "NMRNetplusplus/uninmr/infer.py"
            source.chmod(0o755)
            fixture.capture_committed_inputs()
            result = fixture.run("inference_image.sh", "build")
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual(0o755, fixture.build_evidence()["manifest"]["NMRNetplusplus/uninmr/infer.py"]["mode"])
            self.assert_retired_capture(fixture)

    def test_unrelated_pool_entries_are_not_opened_or_copied_into_image_capture(self):
        for kind in ("link", "fifo", "directory"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                selected = self.author_pool(fixture, fixture.root / ".work/wheelhouse")
                entry = fixture.root / ".work/wheelhouse/unlisted"
                if kind == "link":
                    entry.symlink_to(fixture.root / "requirements/inference.lock")
                elif kind == "fifo":
                    os.mkfifo(entry)
                else:
                    entry.mkdir()
                # This path runs two real pip selections inside the bounded fixture.
                result = fixture.run(
                    "inference_image.sh",
                    "build",
                    additions={"NMRNET_FAKE_VERIFY_WHEELHOUSE": "1"},
                    timeout=30,
                )
                self.assertEqual(0, result.returncode, msg=result.stderr)
                self.assertEqual(set(fixture.build_evidence()["wheelhouse_manifest"]), {selected.name, "unicore-source.tar.gz"})
                self.assertTrue(entry.exists() or entry.is_symlink())
                self.assert_retired_capture(fixture)

    def test_parent_uses_the_captured_declaration_and_rejects_unreadable_or_changed_bytes(self):
        mutation = (
            '() { if [[ -v operation ]] && [[ "$operation" == "input capture" && "$1" == -rf && "$2" == -- ]]; then '
            'local selected="$capture_directory/context/artifacts/nmrnet-source.paths" header; '
            'IFS= read -r header < "$selected"; '
            'printf "%s\\n" "$docker_client_directory" "$capture_directory" > "$NMRNET_DECLARATION_OBSERVATION"; '
            'case "$NMRNET_DECLARATION_MUTATION" in '
            'missing) command rm -f -- "$selected";; '
            'link) printf "outside declaration\\n" > "$repository_root/outside-declaration"; '
            'command rm -f -- "$selected"; ln -s "$repository_root/outside-declaration" "$selected";; '
            'no-newline) printf "%s" "$header" > "$selected";; '
            'nul) printf "%s\\0\\nLICENSE\\nuninmr\\n" "$header" > "$selected";; '
            'unreadable) chmod 000 -- "$selected";; '
            'live-git) printf "%s\\n" "$NMRNET_LATER_REVISION" > "$repository_root/.fake-git/module/HEAD"; '
            'printf "160000 commit %s\\tNMRNetplusplus\\n" "$NMRNET_LATER_REVISION" > "$repository_root/.fake-git/parent/gitlink";; '
            '*) return 97;; esac; fi; command rm "$@"; }'
        )
        for case in ("missing", "link", "no-newline", "nul", "unreadable", "live-git"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                record = fixture.root / ".work/inference/image-id"
                before = record.read_bytes(), record.stat().st_ino
                declaration = fixture.root / "artifacts/nmrnet-source.paths"
                captured_revision = declaration.read_text().splitlines()[0].removeprefix("source_revision ")
                observation = fixture.root / "declaration-observation"
                result = fixture.run(
                    "inference_image.sh",
                    "build",
                    additions={
                        "BASH_FUNC_rm%%": mutation,
                        "NMRNET_DECLARATION_MUTATION": case,
                        "NMRNET_DECLARATION_OBSERVATION": str(observation),
                        "NMRNET_LATER_REVISION": "f" * 40,
                    },
                )
                self.assertTrue(observation.is_file(), msg="Capture did not reach actual confirmed retirement")
                owned = observation.read_text().splitlines()
                self.assertEqual(2, len(owned))
                self.assertTrue(Path(owned[0]).name.startswith("nmrnet-docker-client."))
                self.assertTrue(Path(owned[1]).name.startswith(".build."))
                self.assertTrue(all(not Path(path).exists() for path in owned))
                if case == "live-git":
                    self.assertEqual(0, result.returncode, msg=result.stderr)
                    self.assertEqual("f" * 40 + "\n", (fixture.root / ".fake-git/module/HEAD").read_text())
                    build = next(call["arguments"] for call in fixture.calls() if call["arguments"][:1] == ["build"])
                    self.assertIn("NMRNET_REVISION=" + captured_revision, build)
                    self.assertNotIn("NMRNET_REVISION=" + "f" * 40, build)
                else:
                    self.assertEqual(2, result.returncode, msg=result.stderr)
                    expected = (
                        "Cannot use captured inference source declaration at "
                        + str(Path(owned[1]) / "context/artifacts/nmrnet-source.paths")
                        + ": expected one pinned revision followed by LICENSE and uninmr.\n"
                    )
                    self.assertEqual(expected, result.stderr)
                    self.assertEqual([], fixture.calls(), msg="A failed captured declaration reached an engine")
                    self.assertEqual(before, (record.read_bytes(), record.stat().st_ino))
                    if case == "link":
                        self.assertEqual("outside declaration\n", (fixture.root / "outside-declaration").read_text())
                self.assert_retired_capture(fixture)

    def test_failed_build_retires_capture_and_preserves_the_previous_image_record(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            record = fixture.root / ".work/inference/image-id"
            previous = b"sha256:" + b"c" * 64 + b"\n"
            record.write_bytes(previous)
            result = fixture.run("inference_image.sh", "build", additions={"NMRNET_FAKE_BUILD_STATUS": "7"})
            self.assertEqual(7, result.returncode, msg=result.stderr)
            self.assertEqual(previous, record.read_bytes())
            self.assert_retired_capture(fixture)

    def test_next_action_requires_successful_build_and_cleanup(self):
        cases = (
            ({}, 0),
            ({"NMRNET_FAKE_BUILD_STATUS": "7"}, 7),
            ({"BASH_FUNC_rm%%": '() { if [[ "$*" == *"/.build."* ]]; then return 9; fi; command rm "$@"; }'}, 9),
        )
        for additions, status in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                result = fixture.run("inference_image.sh", "build", additions=additions)
                self.assertEqual(status, result.returncode, result.stderr)
                self.assertEqual(status == 0, "make packages/import" in result.stderr)

    def test_cleanup_failure_preserves_primary_and_reports_its_owned_capture(self):
        for build_status, expected_status in (("0", 9), ("7", 7)):
            with self.subTest(build_status=build_status), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                function = '() { if [[ "$*" == *"/.build."* ]]; then return 9; fi; command rm "$@"; }'
                result = fixture.run(
                    "inference_image.sh",
                    "build",
                    additions={"BASH_FUNC_rm%%": function, "NMRNET_FAKE_BUILD_STATUS": build_status},
                )
                self.assertEqual(expected_status, result.returncode, msg=result.stderr)
                self.assertIn("Cannot confirm owned inference build capture removal; attempted path", result.stderr)
                self.assertEqual(1, len(list((fixture.root / ".work/inference").glob(".build.*"))))

    def test_frozen_verifier_checks_actual_copied_wheel_bytes_before_build(self):
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                shared = tempfile.TemporaryDirectory()
                self.addCleanup(shared.cleanup)
                shared_root = Path(shared.name) / "shared wheels"
                shutil.move(str(fixture.root / ".work/wheelhouse"), shared_root)
                fixture.environment["NMRNET_WHEELHOUSE_INPUT"] = str(shared_root)
                wheel = self.author_pool(fixture, shared_root)
                expected = hashlib.sha256(wheel.read_bytes()).hexdigest()
                fixture_wheel(shared_root, "another-project", "2")
                (shared_root / "private-unrelated.txt").write_text("must not enter image")
                if corrupt:
                    wheel.write_bytes(b"corrupted after receipt")
                result = fixture.run(
                    "inference_image.sh",
                    "build",
                    additions={"NMRNET_FAKE_VERIFY_WHEELHOUSE": "1", "NMRNET_FAKE_MUTATE_INPUT": str(wheel)},
                )
                if corrupt:
                    self.assertNotEqual(0, result.returncode, msg=result.stderr)
                    self.assertFalse(any(call["arguments"][:1] == ["build"] for call in fixture.calls()))
                else:
                    self.assertEqual(0, result.returncode, msg=result.stderr)
                    self.assertEqual(expected, fixture.build_evidence()["wheelhouse_manifest"][wheel.name]["sha256"])
                    self.assertNotIn("private-unrelated.txt", fixture.build_evidence()["wheelhouse_manifest"])
                self.assert_retired_capture(fixture)
