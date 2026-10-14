"""Preserve context, lock publication and immutable-input proof obligations.

Source pins (all 2d4ee50b0359e71900fce9289ea01f46d0f41249):
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/verify_test_image_build_context.py
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/check-image-build-inputs.sh
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_dependency_lock.py
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_checkpoint_release.py

COPY/ignore equality is explicitly discovered here although Magnet's original
verify_test_image_build_context filename is not discovered by unittest. ARG base
pins are checked where launchers bind them. The exact source and deferred-method
crosswalk is in fixtures/containers/source_crosswalk.md. No engine or network is
used: launcher effects are observed through the checked-in executable double.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import re
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repository_checks.fixtures.containers.fixture import ROOT, LauncherFixture
from tools import package_lock


def instructions(source):
    result, pending = [], ""
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            pending += line[:-1].rstrip() + " "
        else:
            result.append(pending + line)
            pending = ""
    if pending:
        raise AssertionError("Cannot inspect Dockerfile context: a continuation has no final instruction.")
    return result


def tool_copies(source):
    copied = set()
    for instruction in instructions(source):
        if not instruction.startswith("COPY "):
            continue
        arguments = shlex.split(instruction)[1:]
        if any(argument.startswith("--from=") for argument in arguments):
            continue
        while arguments and arguments[0].startswith("--"):
            arguments.pop(0)
        if len(arguments) < 2 or arguments[0].startswith("["):
            raise AssertionError("Cannot inspect Dockerfile context: COPY must use an admitted source form.")
        copied.update(path for path in arguments[:-1] if path.startswith("tools/"))
    return copied


def context_tool_leaves(source):
    return {line.strip()[1:] for line in source.splitlines() if line.strip().startswith("!tools/") and line.strip() != "!tools/"}


class TestImageBuildContextPostureTests(unittest.TestCase):
    def test_explicit_tool_copies_match_dockerignore_allowlist(self):
        for name in ("containers/inference/Dockerfile", "containers/packages/Dockerfile.lock"):
            with self.subTest(dockerfile=name):
                dockerfile = (ROOT / name).read_text()
                policy = (ROOT / (name + ".dockerignore")).read_text()
                lines = [line.strip() for line in policy.splitlines() if line.strip() and not line.startswith("#")]
                self.assertIn(lines[0], ("*", "**"))
                self.assertIn("!tools/", lines)
                copied = tool_copies(dockerfile)
                admitted = context_tool_leaves(policy)
                self.assertTrue(copied)
                self.assertEqual(admitted, copied, msg="Missing or excess context admissions: " + name)

    def test_context_detector_rejects_missing_and_excess_leaf_admissions(self):
        source = "COPY tools/one.py tools/two.py /work/\n"
        for policy in ("**\n!tools/\n!tools/one.py\n", "**\n!tools/\n!tools/one.py\n!tools/two.py\n!tools/private.py\n"):
            self.assertNotEqual(tool_copies(source), context_tool_leaves(policy))
        self.assertEqual(tool_copies(source), context_tool_leaves("**\n!tools/\n!tools/one.py\n!tools/two.py\n"))

    def test_context_detector_handles_continuations_and_rejects_unknown_copy_forms(self):
        self.assertEqual(
            {"tools/one.py", "tools/two.py"}, tool_copies("COPY --chown=65532 tools/one.py \\\n tools/two.py /work/\n")
        )
        for source in ('COPY ["tools/one.py", "/work/"]\n', "COPY tools/one.py \\"):
            with self.subTest(source=source), self.assertRaises(AssertionError):
                tool_copies(source)

    def test_build_context_has_only_reviewed_recursive_exceptions(self):
        for name in ("containers/inference/Dockerfile", "containers/packages/Dockerfile.lock"):
            policy = (ROOT / (name + ".dockerignore")).read_text().splitlines()
            admissions = {line[1:] for line in policy if line.startswith("!")}
            self.assertTrue(admissions)
            recursive = {line for line in admissions if "**" in line}
            self.assertEqual(
                recursive, {"NMRNetplusplus/uninmr/**/", "NMRNetplusplus/uninmr/**/*.py"} if "inference" in name else set()
            )
            self.assertTrue(
                all(path.split("/")[0] in {"containers", "tools", "requirements", "NMRNetplusplus"} for path in admissions)
            )
            self.assertFalse(
                any(path in admissions for path in (".git/", ".work/", "secrets/", ".env", "NMRNetplusplus/**", "tools/**"))
            )

    def test_launcher_binds_every_external_base_to_a_full_digest(self):
        for name, variables in (("inference_image.sh", ("python_image",)), ("packages.sh", ("python_image", "uv_image"))):
            source = (ROOT / "tools" / name).read_text()
            for variable in variables:
                values = re.findall(r"^readonly " + variable + r"='([^']+)'$", source, re.MULTILINE)
                self.assertEqual(1, len(values))
                self.assertRegex(values[0], r"^[a-z0-9.-]+/[A-Za-z0-9_./:@-]+@sha256:[0-9a-f]{64}$")
                argument = "UV_IMAGE" if variable == "uv_image" else "PYTHON_BASE"
                self.assertIn('--build-arg "' + argument + "=$" + variable + '"', source)


class ImmutableBuildInputTests(unittest.TestCase):
    def test_capture_preserves_selected_bytes_and_modes_during_worktree_movement(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            selected = fixture.root / "tools/wheelhouse.py"
            selected.chmod(0o644)
            expected = {"sha256": hashlib.sha256(selected.read_bytes()).hexdigest(), "mode": 0o644}
            result = fixture.run("inference_image.sh", "build", additions={"NMRNET_FAKE_MUTATE_INPUT": str(selected)})
            self.assertEqual(0, result.returncode, msg=result.stderr)
            evidence = fixture.build_evidence()
            self.assertEqual(
                evidence["manifest"]["tools/wheelhouse.py"],
                expected,
                msg="The builder read later worktree bytes or mode instead of the admitted snapshot.",
            )
            self.assertNotEqual(evidence["context"], str(fixture.root))
            self.assertFalse(Path(evidence["context"]).exists())

    def test_ignored_python_package_output_rejects_before_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            canary = fixture.root / "tools/__pycache__/private.pyc"
            canary.parent.mkdir()
            canary.write_bytes(b"ignored interpreter output")
            result = fixture.run("inference_image.sh", "build", additions={"NMRNET_FAKE_IGNORED_OUTPUT": "1"})
            self.assertNotEqual(result.returncode, 0, msg="Ignored interpreter output was admitted to the build operation.")
            self.assertFalse(fixture.calls())

    def test_failed_build_preserves_image_record_and_retires_captured_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            record = fixture.root / ".work/inference/image-id"
            before = record.read_bytes(), record.stat().st_ino
            result = fixture.run("inference_image.sh", "build", additions={"NMRNET_FAKE_BUILD_STATUS": "7"})
            self.assertEqual(7, result.returncode)
            self.assertEqual((record.read_bytes(), record.stat().st_ino), before)
            calls = [call["arguments"] for call in fixture.calls()]
            builds = [call for call in calls if call[:1] == ["build"]]
            self.assertEqual(1, len(builds), msg="The injected build failure was not reached.")
            self.assertTrue(fixture.build_evidence()["manifest"], msg="The builder did not read the required captured inputs.")
            self.assertNotEqual(Path(builds[0][-1]), fixture.root)
            self.assertFalse(Path(builds[0][-1]).exists(), msg="Failed build retained a disposable captured context.")
            self.assertEqual({path.name for path in record.parent.iterdir()}, {"image-id"})

    def test_preflight_cancellation_does_not_dispatch_build_or_replace_record(self):
        for status in (130, 143):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                record = fixture.root / ".work/inference/image-id"
                before = record.read_bytes(), record.stat().st_ino
                result = fixture.run("inference_image.sh", "build", additions={"NMRNET_FAKE_GIT_STATUS": str(status)})
                self.assertEqual(result.returncode, status)
                self.assertEqual("", result.stdout)
                self.assertFalse(fixture.calls())
                self.assertEqual((record.read_bytes(), record.stat().st_ino), before)

    def test_import_admits_one_observed_exact_local_image_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            identifier = "sha256:" + "b" * 64
            result = fixture.run("inference_image.sh", "import")
            self.assertEqual(0, result.returncode, msg=result.stderr)
            calls = [call["arguments"] for call in fixture.calls()]
            inspections = [call for call in calls if call[:2] == ["image", "inspect"] and call[-1] == identifier]
            self.assertTrue(inspections, msg="Import used a recorded image without admitting its observed local identity.")
            runs = [call for call in calls if call[:1] == ["run"]]
            self.assertEqual(1, len(runs))
            self.assertIn(identifier, runs[0])
            self.assertLess(calls.index(inspections[-1]), calls.index(runs[0]))

    def test_unavailable_or_forged_local_image_cannot_reach_import(self):
        for additions in (
            {"NMRNET_FAKE_INSPECT_STATUS": "1"},
            {"NMRNET_FAKE_IMAGE_ID": "mutable:tag"},
            {"NMRNET_FAKE_IMAGE_ID": "sha256:" + "c" * 64},
        ):
            with self.subTest(additions=additions), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                result = fixture.run("inference_image.sh", "import", additions=additions)
                self.assertNotEqual(
                    result.returncode, 0, msg="Import accepted unavailable, malformed or replaced observed image identity."
                )
                self.assertEqual("", result.stdout)
                self.assertFalse(any(call["arguments"][:1] == ["run"] for call in fixture.calls()))

    def test_malformed_saved_image_record_rejects_before_engine(self):
        for identity in ("mutable:tag", "sha256:" + "b" * 64 + "\nsha256:" + "c" * 64, "sha256:" + "B" * 64, ""):
            with self.subTest(identity=identity), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                record = fixture.root / ".work/inference/image-id"
                record.write_text(identity)
                before = record.read_bytes(), record.stat().st_ino
                result = fixture.run("inference_image.sh", "import")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual("", result.stdout)
                self.assertEqual((record.read_bytes(), record.stat().st_ino), before)
                self.assertFalse(fixture.calls())

    def test_published_build_identity_is_verified_before_record_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            result = fixture.run("inference_image.sh", "build")
            self.assertEqual(0, result.returncode, msg=result.stderr)
            calls = [call["arguments"] for call in fixture.calls()]
            builds = [call for call in calls if call[:1] == ["build"]]
            self.assertEqual(1, len(builds))
            inspections = [call for call in calls if call[:2] == ["image", "inspect"] and call[-1] == "sha256:" + "b" * 64]
            self.assertTrue(inspections, msg="Build publication did not verify the returned immutable image locally.")
            self.assertGreater(calls.index(inspections[-1]), calls.index(builds[0]))

    def test_malformed_or_multiple_build_identities_preserve_the_predecessor(self):
        for identity in ("mutable:tag", "sha256:" + "b" * 64 + "\nsha256:" + "c" * 64, "x" * (1024 * 1024 + 1)):
            with self.subTest(identity_size=len(identity)), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                record = fixture.root / ".work/inference/image-id"
                before = record.read_bytes(), record.stat().st_ino
                additions = (
                    {"NMRNET_FAKE_BUILD_BYTES": str(len(identity))}
                    if len(identity) > 1024 * 1024
                    else {"NMRNET_FAKE_BUILD_RESULT": identity}
                )
                result = fixture.run("inference_image.sh", "build", additions=additions)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual((record.read_bytes(), record.stat().st_ino), before)
                self.assertEqual({path.name for path in record.parent.iterdir()}, {"image-id"})

    def test_dirty_authored_input_is_rejected_before_engine_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            result = fixture.run("inference_image.sh", "build", additions={"NMRNET_FAKE_DIRTY_ROOT": "1"})
            self.assertNotEqual(result.returncode, 0, msg="Inference build admitted a dirty authored source tree.")
            self.assertFalse(fixture.calls(), msg="A dirty source tree reached the engine before rejection.")

    def test_builder_receives_a_frozen_context_instead_of_the_live_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            result = fixture.run("inference_image.sh", "build")
            self.assertEqual(0, result.returncode, msg=result.stderr)
            builds = [call["arguments"] for call in fixture.calls() if call["arguments"][0] == "build"]
            self.assertEqual(1, len(builds))
            self.assertNotEqual(
                Path(builds[0][-1]).resolve(),
                fixture.root.resolve(),
                msg="A verified build must consume captured immutable inputs, not mutable checkout bytes.",
            )
            self.assertFalse(Path(builds[0][-1]).exists(), msg="Captured context was not retired after the owned build finished.")

    def test_inference_build_and_import_are_offline_without_secret_device_grants(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            for action in ("build", "import"):
                with self.subTest(action=action):
                    before = len(fixture.calls())
                    result = fixture.run("inference_image.sh", action)
                    self.assertEqual(0, result.returncode, msg=result.stderr)
                    runs = [call["arguments"] for call in fixture.calls()[before:] if call["arguments"][:1] == ["run"]]
                    self.assertEqual(1, len(runs))
                    if action == "import":
                        self.assertEqual(runs[0][-1], "sha256:" + "b" * 64)
                        self.assertNotIn("--mount", runs[0])
                    else:
                        self.assertIn("tools.wheelhouse", runs[0])
                        self.assertIn("check", runs[0])
                        mounts = [runs[0][index + 1] for index, argument in enumerate(runs[0]) if argument == "--mount"]
                        self.assertTrue(mounts)
                        self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
            for call in fixture.calls():
                arguments = call["arguments"]
                if arguments[0] not in {"build", "run"}:
                    continue
                self.assertEqual("none", arguments[arguments.index("--network") + 1])
                for forbidden in ("--secret", "--ssh", "--gpus", "--privileged", "--device"):
                    self.assertNotIn(forbidden, arguments)


class LockPublicationTests(unittest.TestCase):
    def test_second_output_install_failure_preserves_both_predecessors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mounts = {name: root / name[1:] for name in ("/input", "/stage", "/output")}
            for path in mounts.values():
                path.mkdir()
            hashes = {}
            for name in package_lock.INPUTS:
                body = name.encode()
                (mounts["/input"] / name).write_bytes(body)
                hashes[name] = hashlib.sha256(body).hexdigest()
            lock = b"candidate==1.0\\\n    --hash=sha256:" + b"a" * 64 + b"\n"
            report = json.dumps({"lock_sha256": hashlib.sha256(lock).hexdigest(), "inputs_sha256": hashes}).encode()
            old = {"inference.lock": b"old lock", "advisories.json": b"old advisory report"}
            for name, candidate in (("inference.lock", lock), ("advisories.json", report)):
                (mounts["/stage"] / name).write_bytes(candidate)
                (mounts["/output"] / name).write_bytes(old[name])
            replace, mkstemp = os.replace, tempfile.mkstemp

            def fail_second(source, destination):
                if Path(destination).name == "advisories.json":
                    raise OSError("injected second output install failure")
                return replace(source, destination)

            with (
                patch.object(package_lock, "Path", side_effect=lambda name: mounts.get(name, Path(name))),
                patch.object(
                    package_lock.tempfile, "mkstemp", side_effect=lambda **kw: mkstemp(**(kw | {"dir": mounts["/output"]}))
                ),
                patch.object(os, "replace", side_effect=fail_second),
                self.assertRaises(OSError),
            ):
                package_lock.apply()
            for name, body in old.items():
                self.assertEqual(
                    (mounts["/output"] / name).read_bytes(),
                    body,
                    msg="A failed lock/advisory transaction published mismatched generations.",
                )
            self.assertEqual({path.name for path in mounts["/output"].iterdir()}, set(old))


if __name__ == "__main__":
    unittest.main()
