"""Exercise operator origin, literal argv, engine ambient state and retirement.

Pinned source: https://github.com/numpde/magnet-deploy/tree/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks
Crosswalk: selected-image-build admission/native failures, operator Git's
networkless preflight, image identity interruption, named-deployment down's
failure truth, checkpoint wrapper input origin and G01 literal-path/no-write
publication. Source-specific method dispositions are retained beside fixtures.
Probes run copied current shell launchers with a finite checked-in engine double;
every helper process remains inside the maintained offline test container.
"""

from tools.container_guard import require_container

require_container()

import os
import re
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repository_checks.fixtures.containers.fixture import ROOT, LauncherFixture


class OperatorInputTests(unittest.TestCase):
    def test_acquisition_requires_explicit_interface_before_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            for additions in ({}, {"WLAN_INTERFACE": "lo"}, {"NMRNET_WLAN_INTERFACE_INPUT": "../escape"}):
                with self.subTest(additions=additions):
                    result = fixture.run("container.sh", "download", additions=additions | {"NMRNET_ARCHIVE_INPUT": "weight.zip"})
                    self.assertEqual(2, result.returncode)
                    self.assertFalse(fixture.calls())

    def test_unknown_and_extra_arguments_fail_before_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            for launcher in ("container.sh", "packages.sh", "inference_image.sh", "baseline.sh"):
                for arguments in (("private-argument",), ("--print", "import", "private-argument")):
                    with self.subTest(launcher=launcher, arguments=arguments):
                        result = fixture.run(launcher, *arguments)
                        self.assertEqual(2, result.returncode)
                        self.assertFalse(fixture.calls())

    def test_ambient_archive_and_stage_cannot_authorize_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            for target, additions in (
                ("weights/download", {"ARCHIVE": "weight.zip"}),
                ("packages/lock/apply", {"STAGE": "lock.fixture"}),
            ):
                with self.subTest(target=target):
                    result = fixture.make(target, "WLAN_INTERFACE=lo", additions=additions)
                    self.assertEqual(2, result.returncode, msg="Ambient write selectors must not authorize " + target)
                    self.assertFalse(fixture.calls(), msg="An unowned write selector reached the engine.")

    def test_make_keeps_literal_interface_syntax_unexpanded(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            sentinel = fixture.root / "expanded"
            result = fixture.make("weights/download", "ARCHIVE=weight.zip", "WLAN_INTERFACE=$(shell touch " + str(sentinel) + ")")
            self.assertEqual(2, result.returncode)
            self.assertFalse(sentinel.exists(), msg="GNU Make executed literal operator input.")
            self.assertFalse(fixture.calls())

    def test_make_keeps_literal_archive_syntax_unexpanded(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            sentinel = fixture.root / "expanded"
            result = fixture.make(
                "weights/download", "WLAN_INTERFACE=lo", "ARCHIVE=$(shell touch " + str(sentinel) + ")weight.zip"
            )
            self.assertEqual(2, result.returncode)
            self.assertFalse(sentinel.exists(), msg="GNU Make executed literal artifact selector syntax.")
            self.assertFalse(fixture.calls())

    def test_check_is_non_mutating_including_input_inode_modes_and_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            downloads = fixture.root / ".work/downloads"
            downloads.mkdir()
            archive = downloads / "weight.zip"
            archive.write_bytes(b"synthetic archive")
            before = (archive.read_bytes(), archive.stat().st_ino, archive.stat().st_mtime_ns, archive.stat().st_mode)
            result = fixture.run("container.sh", "check", additions={"NMRNET_ARCHIVE_INPUT": "weight.zip"})
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual(
                (archive.read_bytes(), archive.stat().st_ino, archive.stat().st_mtime_ns, archive.stat().st_mode), before
            )
            run = fixture.calls()[-1]["arguments"]
            self.assertEqual("none", run[run.index("--network") + 1])
            for index, argument in enumerate(run):
                if argument == "--mount":
                    self.assertTrue(run[index + 1].endswith(",readonly"))

    def test_source_preflight_never_fetches_or_updates_upstream(self):
        # This is the original operator-Git source assertion, not a substitute
        # for immutable snapshot behavior (tested separately in build inputs).
        for name in ("inference_image.sh", "baseline.sh"):
            source = (ROOT / "tools" / name).read_text()
            self.assertEqual(
                [
                    line
                    for line in source.splitlines()
                    if re.match(r"^\s*(?:[A-Z_]+\S*=\S+\s+)*git\b.*\b(?:fetch|pull|checkout|reset)\b", line)
                ],
                [],
            )

    def test_make_does_not_expand_any_raw_selector_when_exporting_recipe_environment(self):
        # Real GNU Make is required: a shell/engine double cannot prove whether
        # implicit command-line exports execute Make functions before recipes.
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            for name in ("ARCHIVE", "STAGE", "WLAN_INTERFACE", "LABEL_ATOL", "ATOL", "RTOL", "RUN_ID"):
                with self.subTest(name=name):
                    sentinel = fixture.root / ("expanded-" + name)
                    result = fixture.make("help", name + "=$(shell touch " + str(sentinel) + ")")
                    self.assertEqual(0, result.returncode, msg=result.stderr)
                    self.assertFalse(sentinel.exists(), msg="GNU Make executed a raw selector while exporting its environment.")
                    self.assertFalse(fixture.calls())

    def test_make_internal_aliases_keep_captured_values_and_public_inputs_stay_unexported(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            public_inputs = {
                "ARCHIVE": "weight.zip",
                "STAGE": "lock.fixture",
                "WLAN_INTERFACE": "lo",
                "LABEL_ATOL": "0.001",
                "ATOL": "0",
                "RTOL": "0.001",
                "RUN_ID": "fixture-run",
            }
            sentinels = []
            arguments = ["inspect-inputs"]
            for name, value in public_inputs.items():
                alias = "NMRNET_" + name + "_INPUT"
                sentinel = fixture.root / ("expanded-" + name)
                sentinels.append(sentinel)
                arguments.extend((name + "=" + value, alias + "=$(shell touch " + str(sentinel) + ")"))
            input_sentinel = fixture.root / "expanded-input"
            sentinels.append(input_sentinel)
            arguments.append("INPUT=$(shell touch " + str(input_sentinel) + ")")

            reported = [
                *((name, "unset") for name in public_inputs),
                *(("NMRNET_" + name + "_INPUT", value) for name, value in public_inputs.items()),
                ("INPUT", "unset"),
            ]
            recipe = "\t@printf '%s\\n' " + " ".join(f'"{name}=$${{{name}-unset}}"' for name, _ in reported)
            with (fixture.root / "Makefile").open("a", encoding="utf-8") as makefile:
                makefile.write("\n.PHONY: inspect-inputs\ninspect-inputs:\n" + recipe + "\n")

            result = fixture.make(*arguments)
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual("".join(name + "=" + value + "\n" for name, value in reported), result.stdout)
            for sentinel in sentinels:
                self.assertFalse(sentinel.exists(), msg="GNU Make expanded a caller-supplied selector expression.")
            self.assertFalse(fixture.calls())

    def test_make_rejects_literal_archive_and_stage_metacharacters_before_engine_or_writes(self):
        for name, target, suffix in (
            ("ARCHIVE", "weights/download", "weight.zip"),
            ("STAGE", "packages/lock/apply", "lock.fixture"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                sentinel = fixture.root / "expanded"
                result = fixture.make(target, "WLAN_INTERFACE=lo", name + "=$(shell touch " + str(sentinel) + ")" + suffix)
                self.assertEqual(2, result.returncode)
                self.assertFalse(sentinel.exists())
                self.assertFalse(fixture.calls())
                self.assertFalse((fixture.root / ".work/downloads").exists())
                self.assertFalse((fixture.root / ".work/packages-stage").exists())

    def test_make_accepts_explicit_archive_and_reviewed_stage_through_literal_aliases(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            downloads = fixture.root / ".work/downloads"
            downloads.mkdir()
            archive = downloads / "weight.zip"
            archive.write_bytes(b"fixture archive")
            result = fixture.make("weights/check", "ARCHIVE=weight.zip")
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual(b"fixture archive", archive.read_bytes())
            arguments = fixture.calls()[-1]["arguments"]
            self.assertEqual("weight.zip", arguments[arguments.index("--name") + 1])
            stage = fixture.root / ".work/packages-stage/lock.fixture"
            stage.mkdir(parents=True)
            (stage / "image-id").write_text("sha256:" + "b" * 64 + "\n")
            result = fixture.make("packages/lock/apply", "STAGE=lock.fixture")
            self.assertEqual(0, result.returncode, msg=result.stderr)
            arguments = fixture.calls()[-1]["arguments"]
            self.assertIn(f"type=bind,src={stage},dst=/stage,readonly", arguments)


class EngineEnvironmentTests(unittest.TestCase):
    def test_baseline_inspects_exact_run_image_and_expected_upstream_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            additions = fixture.baseline_inputs()
            result = fixture.run("baseline.sh", "run", additions=additions)
            self.assertEqual(0, result.returncode, msg=result.stderr)
            calls = [call["arguments"] for call in fixture.calls()]
            inspections = [call for call in calls if call[:2] == ["image", "inspect"]]
            runs = [call for call in calls if call[:1] == ["run"]]
            self.assertEqual(1, len(inspections))
            self.assertEqual(1, len(runs))
            self.assertEqual(inspections[0][-1], "sha256:" + "b" * 64)
            self.assertIn(inspections[0][-1], runs[0])
            self.assertIn("io.nmrnet-repro.upstream-revision", " ".join(inspections[0]))
            self.assertLess(calls.index(inspections[0]), calls.index(runs[0]))

    def test_baseline_missing_malformed_multiple_or_wrong_revision_blocks_execution(self):
        for revision in ("", "malformed", "b" * 40, "a" * 40 + "\n" + "b" * 40, "A" * 40):
            with self.subTest(revision=revision), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                additions = fixture.baseline_inputs() | {"NMRNET_FAKE_IMAGE_REVISION": revision}
                result = fixture.run("baseline.sh", "run", additions=additions)
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertFalse(any(call["arguments"][:1] == ["run"] for call in fixture.calls()))
                self.assertFalse((fixture.root / ".work/baseline/runs").exists())

    def test_baseline_inspection_failure_and_cancellation_preserve_status_without_effects(self):
        for status in (7, 124, 137, 130, 143):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                additions = fixture.baseline_inputs() | {"NMRNET_FAKE_INSPECT_STATUS": str(status)}
                result = fixture.run("baseline.sh", "run", additions=additions)
                self.assertEqual(result.returncode, status)
                self.assertEqual("", result.stdout)
                self.assertFalse(any(call["arguments"][:1] == ["run"] for call in fixture.calls()))
                self.assertFalse((fixture.root / ".work/baseline/runs").exists())

    def test_baseline_inspection_failure_does_not_publish_private_native_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            private = "private-native-inspection-error-marker"
            additions = fixture.baseline_inputs() | {
                "NMRNET_FAKE_INSPECT_STATUS": "7",
                "NMRNET_FAKE_INSPECT_PRIVATE_ERROR": private,
            }
            result = fixture.run("baseline.sh", "run", additions=additions)
            self.assertEqual(7, result.returncode)
            self.assertNotIn(private, result.stdout + result.stderr)
            self.assertFalse(any(call["arguments"][:1] == ["run"] for call in fixture.calls()))

    def test_docker_environment_excludes_ambient_proxy_context_and_credentials(self):
        private = "private-ambient-marker"
        variables = (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_TLS_VERIFY",
            "DOCKER_CERT_PATH",
            "DOCKER_AUTH_CONFIG",
            "DOCKER_CONFIG",
            "BUILDX_CONFIG",
            "BUILDX_BUILDER",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
        )
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            result = fixture.run("inference_image.sh", "import", additions=dict.fromkeys(variables, private))
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertTrue(fixture.calls())
            for call in fixture.calls():
                self.assertNotIn(
                    private,
                    call["environment"].values(),
                    msg="Ambient engine configuration, proxies or credentials reached the engine.",
                )
                self.assertNotIn(private, result.stdout + result.stderr)

    def test_identity_inspection_preserves_bounded_interruption(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            for status in (124, 137, 130, 143):
                with self.subTest(status=status):
                    result = fixture.run(
                        "docker.sh", "require-images", "sha256:" + "b" * 64, additions={"NMRNET_FAKE_INSPECT_STATUS": str(status)}
                    )
                    self.assertEqual(
                        result.returncode, status, msg="Inspection timeout/cancellation was reclassified as an unavailable image."
                    )
                    self.assertEqual("", result.stdout)
                    if status in (130, 143):
                        self.assertEqual("", result.stderr)

    def test_unavailable_acquisition_image_reports_indeterminate_availability_and_private_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            private = "private-acquisition-inspection-error-marker"
            result = fixture.run(
                "docker.sh",
                "require-images",
                "sha256:" + "b" * 64,
                additions={"NMRNET_FAKE_INSPECT_STATUS": "1", "NMRNET_FAKE_INSPECT_PRIVATE_ERROR": private},
            )
            self.assertEqual(2, result.returncode)
            self.assertEqual("", result.stdout)
            self.assertIn("Cannot verify pinned image availability", result.stderr)
            self.assertIn("does not show whether the image is absent or the Docker daemon is unavailable", result.stderr)
            self.assertIn("If the image is missing, preload it", result.stderr)
            self.assertIn("independently interface-bound process", result.stderr)
            self.assertNotIn(private, result.stdout + result.stderr)
            self.assertEqual([["image", "inspect", "sha256:" + "b" * 64]], [call["arguments"] for call in fixture.calls()])

    def test_acquisition_inspection_interruption_suppresses_private_native_diagnostics(self):
        for status in (124, 137, 130, 143):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                private = "private-acquisition-inspection-error-marker"
                result = fixture.run(
                    "docker.sh",
                    "require-images",
                    "sha256:" + "b" * 64,
                    additions={"NMRNET_FAKE_INSPECT_STATUS": str(status), "NMRNET_FAKE_INSPECT_PRIVATE_ERROR": private},
                )
                self.assertEqual(status, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertNotIn(private, result.stdout + result.stderr)
                if status in (130, 143):
                    self.assertEqual("", result.stderr)
                else:
                    self.assertIn("inspection was interrupted (status " + str(status) + ")", result.stderr)
                    self.assertIn("No acquisition was started.", result.stderr)
                self.assertEqual([["image", "inspect", "sha256:" + "b" * 64]], [call["arguments"] for call in fixture.calls()])


class AcquisitionRetirementTests(unittest.TestCase):
    def test_full_container_id_with_nul_receipt_never_authorizes_unadmitted_effects(self):
        # Regression discovered while independently auditing the local CID
        # adaptation: Bash substitution discards NUL before string comparison.
        # Preserve the source's exact-identity authority at the current launcher.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/selected-image-build.py#L343-L370
        for role in ("proxy", "worker"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                self.addCleanup(fixture.dispose)
                result = fixture.run(
                    "docker.sh",
                    "run",
                    "sha256:" + "b" * 64,
                    additions={"NMRNET_WLAN_INTERFACE_INPUT": "lo", "NMRNET_FAKE_NUL_RECEIPT_ROLE": role},
                )
                self.assertEqual(2, result.returncode, msg=result.stderr)
                calls = [call["arguments"] for call in fixture.calls()]
                effects = [call for call in calls if call[0] in {"start", "rm"}]
                # silent-defaults: allow-contract -- A malformed proxy receipt permits no start or removal; a malformed worker receipt permits only the previously admitted proxy's start and removal; proof=repository_checks/test_operator_inputs_port.py::AcquisitionRetirementTests.test_full_container_id_with_nul_receipt_never_authorizes_unadmitted_effects
                expected = [] if role == "proxy" else [["start", "c" * 64], ["rm", "--force", "c" * 64]]
                self.assertEqual(effects, expected)
                receipt = Path((fixture.root / (role + "-cid-path")).read_text())
                # silent-defaults: allow-contract -- This role table expects the fixture's proxy c or worker d identity followed by the deliberately raw NUL; the retained receipt is compared byte-for-byte; proof=repository_checks/test_operator_inputs_port.py::AcquisitionRetirementTests.test_full_container_id_with_nul_receipt_never_authorizes_unadmitted_effects
                self.assertEqual(receipt.read_bytes(), ("c" if role == "proxy" else "d").encode() * 64 + b"\0")
                self.assertTrue(receipt.parent.exists(), msg="Unadmitted CID receipt evidence was discarded.")
                self.assertIn("not admitted", result.stderr)
                self.assertNotIn("ignored null byte", result.stderr)

    def test_corrupted_started_worker_receipt_preserves_cancellation_and_joins_owned_client(self):
        # Exact CID ownership refusal must not strand the already-owned attach
        # client. Unknown container state stays retained for the operator.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_selected_image_build.py#L734-L765
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            self.addCleanup(fixture.dispose)
            damaged = b"unadmitted-worker-receipt\n"
            try:
                result = fixture.run(
                    "docker.sh",
                    "run",
                    "sha256:" + "b" * 64,
                    additions={"NMRNET_WLAN_INTERFACE_INPUT": "lo", "NMRNET_FAKE_HOLD_ATTACH": "1"},
                    corrupt_worker_receipt=damaged,
                    timeout=3,
                )
            except subprocess.TimeoutExpired:
                self.fail(
                    "Cancellation did not join the owned attach client after CID admission failed; the fixture had to retire its process group."
                )
            self.assertEqual(143, result.returncode, msg=result.stderr)
            calls = [call["arguments"] for call in fixture.calls()]
            starts = [call for call in calls if call[0] == "start"]
            self.assertEqual(starts, [["start", "c" * 64], ["start", "--attach", "d" * 64]])
            self.assertEqual([call for call in calls if call[0] == "rm"], [["rm", "--force", "c" * 64]])
            self.assertEqual("", result.stderr, msg="Actual cancellation must not start a new cleanup publisher.")
            receipt = Path((fixture.root / "worker-cid-path").read_text())
            self.assertEqual(receipt.read_bytes(), damaged)
            self.assertTrue(receipt.parent.exists())
            self.assertEqual((fixture.root / "attach-retired").read_text(), (fixture.root / "attach-ready").read_text())
            pid = int((fixture.root / "attach-ready").read_text())
            self.assertFalse(Path(f"/proc/{pid}").exists(), msg="The owned attach client was not joined.")

    def test_complete_worker_cleanup_joins_and_disposes_owned_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            self.addCleanup(fixture.dispose)
            result = fixture.run(
                "docker.sh",
                "run",
                "--pull",
                "never",
                "--read-only",
                "sha256:" + "b" * 64,
                additions={"NMRNET_WLAN_INTERFACE_INPUT": "lo"},
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            calls = [call["arguments"] for call in fixture.calls()]
            creates = [call for call in calls if call[0] == "create"]
            removals = [call for call in calls if call[0] == "rm"]
            self.assertEqual(2, len(creates))
            self.assertEqual(2, len(removals))
            starts = [call for call in calls if call[0] == "start"]
            self.assertEqual(starts, [["start", "c" * 64], ["start", "--attach", "d" * 64]])
            self.assertEqual(
                [call[-1] for call in removals],
                ["d" * 64, "c" * 64],
                msg="Worker retirement must precede proxy retirement using their distinct exact IDs.",
            )
            scratch = Path(creates[0][creates[0].index("--cidfile") + 1]).parent
            self.assertFalse(scratch.exists(), msg="Joined owned acquisition scratch was retained after confirmed cleanup.")

    def test_worker_failure_and_proxy_retirement_failure_both_survive(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            self.addCleanup(fixture.dispose)
            result = fixture.run(
                "docker.sh",
                "run",
                "sha256:" + "b" * 64,
                additions={
                    "NMRNET_WLAN_INTERFACE_INPUT": "lo",
                    "NMRNET_FAKE_WORKER_STATUS": "7",
                    "NMRNET_FAKE_REMOVE_STATUS": "9",
                },
            )
            self.assertEqual(7, result.returncode)
            self.assertIn("removal", result.stderr)
            self.assertIn("worker", result.stderr)
            self.assertIn("proxy", result.stderr)
            creates = [call["arguments"] for call in fixture.calls() if call["arguments"][0] == "create"]
            scratch = Path(creates[0][creates[0].index("--cidfile") + 1]).parent
            self.assertTrue(scratch.exists(), msg="Unconfirmed cleanup deleted the retained ownership evidence.")
            # The fixture owns this directory and can dispose of failed probe
            # evidence after asserting it; production cleanup must retain it.
            shutil.rmtree(scratch)

    def test_malformed_container_identity_never_authorizes_start_or_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            self.addCleanup(fixture.dispose)
            result = fixture.run(
                "docker.sh",
                "run",
                "sha256:" + "b" * 64,
                additions={"NMRNET_WLAN_INTERFACE_INPUT": "lo", "NMRNET_FAKE_PROXY_CID": "foreign-container-name"},
            )
            self.assertNotEqual(result.returncode, 0)
            calls = [call["arguments"] for call in fixture.calls()]
            self.assertFalse(
                any(call[0] in {"start", "rm"} for call in calls), msg="Malformed container identity authorized a native effect."
            )


class NativeCommandPostureTests(unittest.TestCase):
    def retire_recorded_descendant(self, fixture):
        pid = int((fixture.root / "descendant.pid").read_text())
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        except (FileNotFoundError, ProcessLookupError):
            state = "retired"
        if state not in {"Z", "retired"}:
            # Retire only this fixture-recorded child. Return its observed
            # state so cleanup cannot turn a failed retirement proof green.
            os.kill(pid, signal.SIGKILL)
        return state

    def assert_fault_entered(self, fixture, mode):
        marker = fixture.root / "fault-entered"
        self.assertTrue(marker.is_file(), msg="Native fixture did not enter the requested fault: " + mode)
        self.assertEqual(
            marker.read_bytes(),
            (mode + "\n").encode(),
            msg="Native fixture entered a different fault than the retirement proof requested.",
        )

    def require_native_deadline_adapter(self, fixture):
        preview = fixture.run("inference_image.sh", "--print", "import")
        self.assertEqual(0, preview.returncode, msg=preview.stderr)
        self.assertFalse(fixture.calls(), msg="Deadline proof admission dispatched an engine command.")
        # A reviewed test adapter must bind the source suite's explicit short
        # budget to an actual supported target control. Its shape belongs to
        # this checker, not to a prescribed production module or supervisor.
        adapter = getattr(fixture, "native_deadline_adapter", None)
        self.assertTrue(
            callable(adapter),
            msg="Cannot prove native deadline and descendant retirement: no reviewed adapter binds an explicit "
            "test budget to a supported tools/inference_image.sh deadline control. Its current --print "
            "import lane exposes no owned deadline control; EOF and descendant scenarios were not dispatched. "
            "The fixture outer timeout is only checker cleanup, not an operator deadline policy.",
        )
        return adapter

    def test_output_eof_does_not_allow_native_execution_to_outlive_the_probe_budget(self):
        # Timing calibration: the finite engine imports Python before entering
        # its fault; 0.1s can expire during startup. Require actual fault entry
        # under 1s, keeping the fixture's 3s cleanup bound and direct native
        # 0.1s tests unchanged. Original behavior: Magnet selected-image-build
        # at 2d4ee50b0359e71900fce9289ea01f46d0f41249,
        # repository_checks/test_selected_image_build.py#L190-L199.
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            adapter = self.require_native_deadline_adapter(fixture)
            try:
                result = adapter(fixture, additions={"NMRNET_FAKE_HOLD_MODE": "eof"}, budget=1)
            except subprocess.TimeoutExpired:
                self.fail(
                    "Native execution closed its output but remained unbounded; the contained fixture had to retire its process group."
                )
            self.assert_fault_entered(fixture, "eof")
            self.assertEqual(124, result.returncode)

    def test_failed_leader_and_held_pipe_retire_surviving_native_descendant(self):
        # The same calibrated budget admits Python startup; the entry marker
        # proves spawning/recording the child happened before leader status 7.
        # Original retirement intent: Magnet's same pinned source, L208-L232.
        for mode in ("failed-leader", "held-pipe"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                adapter = self.require_native_deadline_adapter(fixture)
                timed_out = False
                try:
                    result = adapter(fixture, additions={"NMRNET_FAKE_HOLD_MODE": mode}, budget=1)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    result = None
                try:
                    self.assert_fault_entered(fixture, mode)
                except AssertionError as marker_failure:
                    # Fault admission can fail after spawning. Preserve that
                    # failure while retiring an already-recorded exact child,
                    # before this temporary context removes its PID receipt.
                    if (fixture.root / "descendant.pid").is_file():
                        try:
                            self.retire_recorded_descendant(fixture)
                        except (OSError, ValueError) as retirement_failure:
                            marker_failure.add_note("Recorded child cleanup also failed: " + type(retirement_failure).__name__)
                    raise
                state = self.retire_recorded_descendant(fixture)
                self.assertFalse(
                    timed_out,
                    msg="A descendant-held pipe bypassed owned native retirement; the fixture enforced its outer deadline.",
                )
                self.assertIn(state, {"Z", "retired"}, msg="Failed native leader left its descendant running.")
                self.assertEqual(7, result.returncode)

    def test_expected_status_cannot_substitute_for_actual_fault_entry(self):
        children = []

        def expected_status_without_fault(fixture, *, additions, budget):
            self.assertEqual(budget, 1)
            mode = additions["NMRNET_FAKE_HOLD_MODE"]
            marker = os.environ["NMRNET_TEST_INVALID_MARKER"]
            if marker.endswith("-with-child"):
                child = subprocess.Popen(["sleep", "60"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                children.append(child)
                (fixture.root / "descendant.pid").write_text(str(child.pid))
            if marker.startswith("wrong"):
                (fixture.root / "fault-entered").write_bytes(b"wrong-mode\n")
            # silent-defaults: allow-contract -- This status-only counterexample returns EOF timeout124 or failed-leader/held-pipe status7; the probes must still fail without the exact fault-entry marker; proof=repository_checks/test_operator_inputs_port.py::NativeCommandPostureTests.test_expected_status_cannot_substitute_for_actual_fault_entry
            return subprocess.CompletedProcess([], 124 if mode == "eof" else 7, "", "")

        probes = (
            "test_output_eof_does_not_allow_native_execution_to_outlive_the_probe_budget",
            "test_failed_leader_and_held_pipe_retire_surviving_native_descendant",
        )
        for probe in probes:
            for marker in ("missing", "wrong", "missing-with-child", "wrong-with-child"):
                if probe == "test_output_eof_does_not_allow_native_execution_to_outlive_the_probe_budget" and marker.endswith(
                    "-with-child"
                ):
                    continue
                with (
                    self.subTest(probe=probe, marker=marker),
                    patch.dict(os.environ, {"NMRNET_TEST_INVALID_MARKER": marker}),
                    patch.object(LauncherFixture, "native_deadline_adapter", staticmethod(expected_status_without_fault)),
                ):
                    children.clear()
                    try:
                        result = unittest.TestResult()
                        NativeCommandPostureTests(probe).run(result)
                        self.assertEqual([], result.errors)
                        self.assertTrue(result.failures, msg="Expected native status bypassed fault-entry proof.")
                        for _case, failure in result.failures:
                            self.assertRegex(failure, "Native fixture (did not enter|entered a different fault)")
                        for child in children:
                            self.assertEqual(
                                child.wait(timeout=2),
                                -signal.SIGKILL,
                                msg="A bad fault-entry marker skipped exact recorded-child cleanup.",
                            )
                    finally:
                        # These Popen handles own only children of this proof;
                        # retire them even if the cleanup assertion itself fails.
                        for child in children:
                            if child.poll() is None:
                                child.kill()
                            child.wait(timeout=2)

    def test_native_failure_keeps_private_output_out_of_publishable_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            private = "private-native-command-output-marker"
            result = fixture.run(
                "inference_image.sh", "import", additions={"NMRNET_FAKE_ENGINE_STATUS": "7", "NMRNET_FAKE_OUTPUT": private}
            )
            self.assertEqual(7, result.returncode)
            self.assertNotIn(
                private,
                result.stdout + result.stderr,
                msg="Failed native output was published instead of being retained as private evidence.",
            )

    def test_success_drains_output_without_exceeding_original_log_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = LauncherFixture(Path(directory))
            bound = 8 * 1024 * 1024  # original MAX_COMMAND_LOG_BYTES
            result = fixture.run("inference_image.sh", "import", additions={"NMRNET_FAKE_OUTPUT_BYTES": str(bound + 1)})
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertLessEqual(
                len(result.stdout.encode()) + len(result.stderr.encode()),
                bound,
                msg="A native command exceeded the original capped evidence bound.",
            )

    def test_cancellation_and_cleanup_failure_preserve_primary_status_and_evidence(self):
        for status in (130, 143):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                fixture = LauncherFixture(Path(directory))
                self.addCleanup(fixture.dispose)
                result = fixture.run(
                    "docker.sh",
                    "run",
                    "sha256:" + "b" * 64,
                    additions={
                        "NMRNET_WLAN_INTERFACE_INPUT": "lo",
                        "NMRNET_FAKE_WORKER_STATUS": str(status),
                        "NMRNET_FAKE_REMOVE_STATUS": "9",
                    },
                )
                self.assertEqual(result.returncode, status)
                self.assertIn("removal", result.stderr)
                creates = [call["arguments"] for call in fixture.calls() if call["arguments"][0] == "create"]
                scratch = Path(creates[0][creates[0].index("--cidfile") + 1]).parent
                self.assertTrue(scratch.exists())
                shutil.rmtree(scratch)


if __name__ == "__main__":
    unittest.main()
