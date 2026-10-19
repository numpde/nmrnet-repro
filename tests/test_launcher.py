#!/usr/bin/env python3
"""Check the supported launcher's actual argv without engine access."""

import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class LauncherTests(unittest.TestCase):
    def test_pdb_retirement_target_declares_its_fixed_separate_container_scenario(self):
        result = subprocess.run(
            ["make", "--dry-run", "test/integration/provider-geometry-worker/pdb"], capture_output=True, text=True, check=False
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["bash tools/worker_qualification.sh geometry-pdb"], result.stdout.splitlines())

    def test_native_geometry_retirement_target_declares_each_fixed_scenario(self):
        result = subprocess.run(
            ["make", "--dry-run", "test/integration/provider-native-geometry-worker"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            [
                f"bash tools/worker_qualification.sh geometry-native-{scenario}"
                for scenario in ("mol", "sdf", "rejected", "failure", "timeout")
            ],
            result.stdout.splitlines(),
        )

    def command(self, lane, archive=None):
        environment = dict(os.environ)
        # silent-defaults: allow-pop-default -- Remove the ambient archive before testing explicit archive selection; proof=tests/test_launcher.py::LauncherTests.test_download_requires_explicit_archive_and_only_writes_downloads
        environment.pop("ARCHIVE", None)
        # silent-defaults: allow-pop-default -- Revoke inherited forwarded selection before omission and explicit archive cases; proof=tests/test_launcher.py::LauncherTests.test_download_requires_explicit_archive_and_only_writes_downloads
        environment.pop("NMRNET_ARCHIVE_INPUT", None)
        environment["NMRNET_WLAN_INTERFACE_INPUT"] = "lo"
        if archive is not None:
            environment["NMRNET_ARCHIVE_INPUT"] = archive
        return subprocess.run(
            ["bash", "tools/container.sh", "--print", lane], env=environment, text=True, capture_output=True, check=False
        )

    def test_test_lane_is_offline_and_reads_only_authored_inputs(self):
        result = self.command("test")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        arguments = shlex.split(result.stdout)
        self.assertEqual("none", arguments[arguments.index("--network") + 1])
        self.assertIn("--read-only", arguments)
        self.assertIn("no-new-privileges:true", arguments)
        self.assertIn("NMRNET_IN_CONTAINER=1", arguments)
        self.assertIn("--init", arguments)
        mounts = [arguments[i + 1] for i, item in enumerate(arguments) if item == "--mount"]
        self.assertEqual(4, len(mounts))
        self.assertEqual(
            {"/workspace/tools", "/workspace/tests", "/workspace/nmrnet_inference", "/workspace/nmrnet_provider"},
            {mount.split("dst=", 1)[1].split(",", 1)[0] for mount in mounts},
        )
        self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
        self.assertNotIn("/var/run/docker.sock", " ".join(mounts))

    def checker_checkout(self, directory, *, run_status=None):
        checkout = Path(directory)
        (checkout / "tools").mkdir()
        (checkout / "tests").mkdir()
        (checkout / "nmrnet_inference").mkdir()
        (checkout / "nmrnet_provider").mkdir()
        (checkout / "repository_checks").mkdir()
        (checkout / "Makefile").write_text("help:\n")
        (checkout / "tools/container.sh").write_bytes(Path("tools/container.sh").read_bytes())
        (checkout / "tools/docker_client.sh").write_bytes(Path("tools/docker_client.sh").read_bytes())
        (checkout / "tools/inference_native.sh").write_bytes(Path("tools/inference_native.sh").read_bytes())
        (checkout / ".work/checker").mkdir(parents=True)
        (checkout / "bin").mkdir()
        # These fake owners exercise launcher admission and cleanup without an
        # engine or Git dependency in the base-image unit lane. The separate Git
        # suite proves the real scaffold against committed objects.
        helper = checkout / "tools/check_git_inputs.sh"
        helper.write_text('#!/bin/bash\nmkdir -p -- "$2/objects" "$2/modules/nmrnet/objects"\n')
        docker = checkout / "bin/docker"
        # The source fixture is executable on its read-only mount; /tmp is noexec.
        docker.symlink_to(Path(__file__).resolve())
        environment = {
            **os.environ,
            "PATH": f"{checkout / 'bin'}:{os.environ['PATH']}",
            "PYTHONPATH": str(Path.cwd()),
            "ENGINE_LOG": str(checkout / "engine-log"),
            "OBSERVED_IMAGE": "sha256:" + "a" * 64,
            # Ordinary inspection belongs to this checkout; fault cases
            # explicitly replace the modeled output kind.
            "FAKE_INSPECT_OUTPUT": "normal",
        }
        # silent-defaults: allow-pop-default -- Admission-only fixtures declare no worker outcome; revoke inherited status before an explicit scenario may supply it; proof=tests/test_launcher.py::LauncherTests.test_repository_worker_requires_declared_status_despite_ambient_success
        environment.pop("FAKE_RUN_STATUS", None)
        if run_status is not None:
            environment["FAKE_RUN_STATUS"] = str(run_status)
        return checkout, environment

    def test_provider_lane_selects_only_provider_input_tests_without_extra_authority(self):
        result = self.command("test/unit/provider")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        arguments = shlex.split(result.stdout)
        self.assertEqual(["-p", "test_provider_*.py", "-v"], arguments[-3:])
        self.assertEqual("none", arguments[arguments.index("--network") + 1])
        self.assertIn("--read-only", arguments)
        mounts = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--mount"]
        self.assertEqual(4, len(mounts))
        self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
        self.assertNotIn("docker.sock", " ".join(mounts))

    def test_geometry_lane_selects_its_focused_process_tests_offline(self):
        result = self.command("test/unit/provider-geometry-worker")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        arguments = shlex.split(result.stdout)
        self.assertEqual(["-p", "test_provider_geometry_worker.py", "-v"], arguments[-3:])
        self.assertEqual("none", arguments[arguments.index("--network") + 1])
        self.assertIn("--init", arguments)
        self.assertIn("--read-only", arguments)
        mounts = [arguments[i + 1] for i, value in enumerate(arguments) if value == "--mount"]
        self.assertEqual(4, len(mounts))
        self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
        self.assertNotIn("docker.sock", " ".join(mounts))

    def test_pure_reader_lanes_select_only_their_tests_without_native_or_engine_grants(self):
        for source_format in ("pdb", "mol2"):
            with self.subTest(source_format=source_format):
                result = self.command(f"test/unit/provider/{source_format}")
                self.assertEqual(0, result.returncode, result.stderr)
                arguments = shlex.split(result.stdout)
                self.assertEqual(["-p", f"test_provider_{source_format}.py", "-v"], arguments[-3:])
                self.assertEqual("none", arguments[arguments.index("--network") + 1])
                self.assertIn("--read-only", arguments)
                mounts = [arguments[i + 1] for i, argument in enumerate(arguments) if argument == "--mount"]
                self.assertEqual(4, len(mounts))
                self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
                self.assertNotIn("docker.sock", " ".join(mounts))

    def test_repository_print_has_no_record_scaffold_or_engine_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout, environment = self.checker_checkout(temporary)
            result = subprocess.run(
                ["bash", str(checkout / "tools/container.sh"), "--print", "test/repository"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertIn("CHECKER_IMAGE_ID_FROM_RECORD", shlex.split(result.stdout))
            self.assertFalse((checkout / "engine-log").exists())
            self.assertEqual([checkout / ".work/checker"], list((checkout / ".work").iterdir()))

    def test_repository_rejects_nonexact_image_records_before_engine_access(self):
        image = b"sha256:" + b"a" * 64
        with tempfile.TemporaryDirectory() as temporary:
            checkout, environment = self.checker_checkout(temporary)
            record = checkout / ".work/checker/image-id"
            for data in (
                b"python:latest\n",
                image,
                image + b"\n\n",
                image + b"\x00\n",
                image[:20] + b"\x00" + image[21:] + b"\n",
                image + b"\r\n",
            ):
                with self.subTest(data=data):
                    record.write_bytes(data)
                    result = subprocess.run(
                        ["bash", str(checkout / "tools/container.sh"), "test/repository"],
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(2, result.returncode, msg=result.stderr)
                    self.assertFalse((checkout / "engine-log").exists())
            record.unlink()
            (checkout / "target").write_bytes(image + b"\n")
            record.symlink_to(checkout / "target")
            result = subprocess.run(
                ["bash", str(checkout / "tools/container.sh"), "test/repository"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, result.returncode, msg=result.stderr)
            self.assertFalse((checkout / "engine-log").exists())

    def test_repository_requires_observed_immutable_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout, environment = self.checker_checkout(temporary)
            (checkout / ".work/checker/image-id").write_text("sha256:" + "a" * 64 + "\n")
            environment["OBSERVED_IMAGE"] = "sha256:" + "b" * 64
            result = subprocess.run(
                ["bash", str(checkout / "tools/container.sh"), "test/repository"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(2, result.returncode, msg=result.stderr)
            self.assertIn("identity differs", result.stderr)
            self.assertEqual(1, len((checkout / "engine-log").read_text().splitlines()))
            self.assertEqual([checkout / ".work/checker"], list((checkout / ".work").iterdir()))

    def test_repository_failure_keeps_status_and_removes_owned_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout, environment = self.checker_checkout(temporary, run_status=19)
            image = "sha256:" + "a" * 64
            (checkout / ".work/checker/image-id").write_text(image + "\n")
            result = subprocess.run(
                ["bash", str(checkout / "tools/container.sh"), "test/repository"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(19, result.returncode, msg=result.stderr)
            invocations = (checkout / "engine-log").read_text().splitlines()
            self.assertEqual(2, len(invocations))
            self.assertIn(image + " bash -eu -c", invocations[-1])
            self.assertEqual([checkout / ".work/checker"], list((checkout / ".work").iterdir()))

    def test_repository_worker_requires_declared_status_despite_ambient_success(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"FAKE_RUN_STATUS": "0"}):
            checkout, environment = self.checker_checkout(temporary)
            image = "sha256:" + "a" * 64
            (checkout / ".work/checker/image-id").write_text(image + "\n")
            environment["FAKE_INSPECT_OUTPUT"] = "normal"
            result = subprocess.run(
                ["bash", str(checkout / "tools/container.sh"), "test/repository"],
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(1, result.returncode, msg=result.stderr)
            self.assertEqual("", result.stdout)
            self.assertIn("KeyError: 'FAKE_RUN_STATUS'", result.stderr)
            invocations = (checkout / "engine-log").read_text().splitlines()
            self.assertEqual(["image", "run"], [shlex.split(line)[4] for line in invocations])
            self.assertIn(image + " bash -eu -c", invocations[-1])

    def test_inspection_output_kind_is_owned_despite_ambient_fault(self):
        image = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"FAKE_INSPECT_OUTPUT": "nul"}):
            checkout, environment = self.checker_checkout(temporary, run_status=0)
            (checkout / ".work/checker/image-id").write_text(image + "\n")
            result = subprocess.run(
                ["bash", str(checkout / "tools/container.sh"), "test/repository"],
                env=environment,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            self.assertEqual("", result.stdout)
            self.assertEqual("", result.stderr)
            invocations = (checkout / "engine-log").read_text().splitlines()
            self.assertEqual(["image", "run"], [shlex.split(line)[4] for line in invocations])

    def test_missing_inspection_output_kind_preserves_logged_attempt_and_blocks_worker(self):
        image = "sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as temporary:
            checkout, environment = self.checker_checkout(temporary, run_status=0)
            (checkout / ".work/checker/image-id").write_text(image + "\n")
            del environment["FAKE_INSPECT_OUTPUT"]
            result = subprocess.run(
                ["bash", str(checkout / "tools/container.sh"), "test/repository"],
                env=environment,
                text=True,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(1, result.returncode, msg=result.stderr)
            self.assertEqual("", result.stdout)
            invocations = (checkout / "engine-log").read_text().splitlines()
            self.assertEqual(["image"], [shlex.split(line)[4] for line in invocations])
            retained = list((checkout / ".work/checker").glob(".inspect.*"))
            self.assertEqual(1, len(retained))
            self.assertEqual(b"", (retained[0] / "observed-id").read_bytes())
            self.assertIn(b"KeyError: 'FAKE_INSPECT_OUTPUT'", (retained[0] / "diagnostic").read_bytes())
            self.assertNotIn("KeyError", result.stderr)

    def test_repository_rejects_nonexact_observed_receipts_and_keeps_native_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout, environment = self.checker_checkout(temporary)
            (checkout / ".work/checker/image-id").write_text("sha256:" + "a" * 64 + "\n")
            for suffix, expected_status in (
                ("nul", 2),
                ("extra-lf", 2),
                ("missing-lf", 2),
                ("native-failure", 7),
                ("oversized", None),
                ("hang", 124),
            ):
                with self.subTest(suffix=suffix):
                    environment["FAKE_INSPECT_OUTPUT"] = suffix
                    result = subprocess.run(
                        ["bash", str(checkout / "tools/container.sh"), "test/repository"],
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    if expected_status is None:
                        self.assertNotEqual(result.returncode, 0, msg=result.stderr)
                    else:
                        self.assertEqual(expected_status, result.returncode, msg=result.stderr)
                    self.assertNotIn("private fixture diagnostic", result.stderr)
                    # Failed inspection is operator evidence after retirement;
                    # keep its literal private cause, not a live worker input.
                    retained = list((checkout / ".work/checker").glob(".inspect.*"))
                    self.assertEqual(1, len(retained))
                    evidence = retained[0]
                    self.assertTrue(evidence.is_dir())
                    self.assertFalse(evidence.is_symlink())
                    self.assertEqual(0o700, evidence.stat().st_mode & 0o777)
                    observed = evidence / "observed-id"
                    diagnostic = evidence / "diagnostic"
                    for receipt in (observed, diagnostic):
                        self.assertTrue(receipt.is_file())
                        self.assertFalse(receipt.is_symlink())
                        self.assertEqual(0o600, receipt.stat().st_mode & 0o777)
                        self.assertLessEqual(receipt.stat().st_size, 4096)
                    raw = observed.read_bytes()
                    if suffix == "oversized":
                        self.assertGreater(len(raw), 0)
                        self.assertEqual(b"x" * len(raw), raw)
                    else:
                        expected = {
                            "nul": ("sha256:" + "a" * 64).encode() + b"\0\n",
                            "extra-lf": ("sha256:" + "a" * 64).encode() + b"\n\n",
                            "missing-lf": ("sha256:" + "a" * 64).encode(),
                            "native-failure": b"",
                            "hang": b"",
                        }
                        self.assertEqual(expected[suffix], raw)
                    if suffix == "native-failure":
                        self.assertEqual(b"private fixture diagnostic\n", diagnostic.read_bytes())
                    observed.unlink()
                    diagnostic.unlink()
                    evidence.rmdir()
                    log = checkout / "engine-log"
                    self.assertEqual(1, len(log.read_text().splitlines()))
                    log.unlink()

    def test_required_source_links_reject_before_engine_or_print_grants(self):
        for lane, names in (
            ("test", ("tools", "tests", "nmrnet_inference", "nmrnet_provider")),
            ("test/repository", ("tools", "repository_checks", "Makefile", "nmrnet_provider")),
        ):
            for name in names:
                with self.subTest(lane=lane, name=name), tempfile.TemporaryDirectory() as temporary:
                    checkout, environment = self.checker_checkout(temporary)
                    (checkout / ".work/checker/image-id").write_text("sha256:" + "a" * 64 + "\n")
                    private = checkout / "private-input"
                    (checkout / name).rename(private)
                    (checkout / name).symlink_to(private, target_is_directory=private.is_dir())
                    for flags in ([], ["--print"]):
                        result = subprocess.run(
                            ["bash", str(checkout / "tools/container.sh"), *flags, lane],
                            env=environment,
                            text=True,
                            capture_output=True,
                            check=False,
                        )
                        self.assertEqual(2, result.returncode, msg=result.stderr)
                        self.assertIn("without a symlink", result.stderr)
                        self.assertEqual("", result.stdout)
                        self.assertFalse((checkout / "engine-log").exists())

    def test_download_requires_explicit_archive_and_only_writes_downloads(self):
        self.assertNotEqual(self.command("download").returncode, 0)
        self.assertNotEqual(self.command("download", "../weight.zip").returncode, 0)
        result = self.command("download", "weight.zip")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        arguments = shlex.split(result.stdout.splitlines()[-2])
        self.assertEqual("host", arguments[arguments.index("--network") + 1])
        mounts = [arguments[i + 1] for i, item in enumerate(arguments) if item == "--mount"]
        writable = [mount for mount in mounts if not mount.endswith(",readonly")]
        self.assertEqual([f"type=bind,src={Path.cwd()}/.work/downloads,dst=/downloads"], writable)

    def test_ambient_archive_does_not_select_a_download(self):
        environment = {**os.environ, "ARCHIVE": "weight.zip", "NMRNET_WLAN_INTERFACE_INPUT": "lo"}
        # silent-defaults: allow-pop-default -- Revoke forwarded selection so ambient ARCHIVE alone cannot select a download; proof=tests/test_launcher.py::LauncherTests.test_ambient_archive_does_not_select_a_download
        environment.pop("NMRNET_ARCHIVE_INPUT", None)
        result = subprocess.run(
            ["bash", "tools/container.sh", "--print", "download"], env=environment, text=True, capture_output=True, check=False
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("make command line", result.stderr)
        self.assertEqual("", result.stdout)

    def test_check_lane_reads_downloads_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            (checkout / "tools").mkdir()
            (checkout / "tools/container.sh").write_bytes(Path("tools/container.sh").read_bytes())
            (checkout / "tools/docker_client.sh").write_bytes(Path("tools/docker_client.sh").read_bytes())
            (checkout / "tools/docker.sh").write_bytes(Path("tools/docker.sh").read_bytes())
            (checkout / ".work/downloads").mkdir(parents=True)
            (checkout / "tools/inference_native.sh").write_bytes(Path("tools/inference_native.sh").read_bytes())
            result = subprocess.run(
                ["bash", str(checkout / "tools/container.sh"), "--print", "check"],
                env={**os.environ, "NMRNET_ARCHIVE_INPUT": "weight.zip"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            arguments = shlex.split(result.stdout)
            self.assertEqual("none", arguments[arguments.index("--network") + 1])
            mounts = [arguments[i + 1] for i, item in enumerate(arguments) if item == "--mount"]
            self.assertEqual(4, len(mounts))
            self.assertTrue(all(mount.endswith(",readonly") for mount in mounts))
            self.assertIn("NMRNET_IN_CONTAINER=1", arguments)
            self.assertEqual("tools.artifacts", arguments[arguments.index("-m") + 1])
            self.assertIn(
                f"type=bind,src={checkout}/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly", mounts
            )
            self.assertIn(f"type=bind,src={checkout}/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly", mounts)

    def test_symlink_staging_is_rejected_before_engine_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            (checkout / "tools").mkdir()
            (checkout / "tools/container.sh").write_bytes(Path("tools/container.sh").read_bytes())
            (checkout / "tools/docker_client.sh").write_bytes(Path("tools/docker_client.sh").read_bytes())
            (checkout / "tools/docker.sh").write_bytes(Path("tools/docker.sh").read_bytes())
            (checkout / ".work").symlink_to(checkout / "elsewhere", target_is_directory=True)
            (checkout / "tools/inference_native.sh").write_bytes(Path("tools/inference_native.sh").read_bytes())
            result = subprocess.run(
                ["bash", str(checkout / "tools/container.sh"), "--print", "download"],
                env={**os.environ, "NMRNET_ARCHIVE_INPUT": "weight.zip", "NMRNET_WLAN_INTERFACE_INPUT": "lo"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("without a symlink", result.stderr)

    def test_extract_reads_selected_inputs_offline_and_only_writes_prepared(self):
        with tempfile.TemporaryDirectory() as temporary:
            checkout = Path(temporary)
            (checkout / "tools").mkdir()
            (checkout / "tools/container.sh").write_bytes(Path("tools/container.sh").read_bytes())
            (checkout / "tools/docker_client.sh").write_bytes(Path("tools/docker_client.sh").read_bytes())
            (checkout / ".work/downloads").mkdir(parents=True)
            (checkout / "tools/inference_native.sh").write_bytes(Path("tools/inference_native.sh").read_bytes())
            (checkout / "artifacts").mkdir()
            (checkout / "artifacts/weight.selection.json").write_text("{}")
            result = subprocess.run(
                ["bash", str(checkout / "tools/container.sh"), "--print", "extract"],
                env={**os.environ, "NMRNET_ARCHIVE_INPUT": "weight.zip"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, result.returncode, msg=result.stderr)
            arguments = shlex.split(result.stdout)
            self.assertEqual("none", arguments[arguments.index("--network") + 1])
            mounts = [arguments[i + 1] for i, item in enumerate(arguments) if item == "--mount"]
            self.assertEqual(6, len(mounts))
            writable = [mount for mount in mounts if not mount.endswith(",readonly")]
            self.assertEqual([f"type=bind,src={checkout}/.work/prepared,dst=/prepared"], writable)
            self.assertIn(f"type=bind,src={checkout}/.work/downloads,dst=/downloads,readonly", mounts)
            self.assertIn(f"type=bind,src={checkout}/artifacts/weight.selection.json,dst=/inputs/selection.json,readonly", mounts)
            self.assertEqual("/prepared/weight", arguments[arguments.index("--output") + 1])
            (checkout / ".work/prepared").symlink_to(checkout / "elsewhere", target_is_directory=True)
            denied = subprocess.run(
                ["bash", str(checkout / "tools/container.sh"), "--print", "extract"],
                env={**os.environ, "NMRNET_ARCHIVE_INPUT": "weight.zip"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(denied.returncode, 0)
            self.assertIn("without a symlink", denied.stderr)


if __name__ == "__main__":
    if Path(sys.argv[0]).name == "docker":
        from tools.container_guard import require_container

        require_container()
        arguments = sys.argv[1:]
        if arguments[:1] != ["--config"] or arguments[2:4] != ["--host", "unix:///var/run/docker.sock"]:
            raise SystemExit(97)
        with Path(os.environ["ENGINE_LOG"]).open("a") as output:
            output.write(" ".join(arguments) + "\n")
        if arguments[4:6] == ["image", "inspect"]:
            output = os.environ["OBSERVED_IMAGE"].encode("ascii")
            kind = os.environ["FAKE_INSPECT_OUTPUT"]
            if kind == "native-failure":
                sys.stderr.write("private fixture diagnostic" + "\n")
                raise SystemExit(7)
            if kind == "hang":
                import time

                time.sleep(60)
            if kind == "oversized":
                output = b"x" * 100000
            # silent-defaults: allow-contract -- Named malformed modes select NUL, extra or missing LF; other modeled output keeps the ordinary single LF; proof=tests/test_launcher.py::LauncherTests.test_repository_rejects_nonexact_observed_receipts_and_keeps_native_failure
            suffix = {"nul": b"\0\n", "extra-lf": b"\n\n", "missing-lf": b""}.get(kind, b"\n")
            sys.stdout.buffer.write(output + suffix)
            sys.stdout.buffer.flush()
        elif arguments[4:5] == ["run"]:
            raise SystemExit(int(os.environ["FAKE_RUN_STATUS"]))
        else:
            raise SystemExit(97)
    else:
        unittest.main()
