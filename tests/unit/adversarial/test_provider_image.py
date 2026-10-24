"""Unverified dependency bytes never reach the controller image installer."""

from tools.container_guard import require_container

require_container()

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.unit.provider_image_ports import ImageSource
from tests.unit.wheel_pool_fixtures import locked, wheel

from tools.provider_environment import install_provider, install_provider_into


class ProviderDependencyAdmissionTests(unittest.TestCase):
    def test_existing_scientific_dependency_conflict_is_refused_before_any_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            (source / "requirements").mkdir(parents=True)
            environment = Path(directory) / "environment"
            subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(environment)], check=True)
            python = environment / "bin/python"
            site = Path(subprocess.check_output(
                [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True,
            ).strip())
            metadata = site / "example-1.dist-info"
            metadata.mkdir()
            original = b"Metadata-Version: 2.1\nName: example\nVersion: 1\n"
            (metadata / "METADATA").write_bytes(original)
            wheels = Path(directory) / "wheels"
            wheels.mkdir()
            candidate = wheel(wheels, "example", "2")
            locked(source / "requirements/provider-core.lock", [("example", "2", [candidate])])
            with (
                patch("tools.provider_environment.subprocess.run", wraps=subprocess.run) as effects,
                self.assertRaisesRegex(ValueError, "installed example==1 differs from core lock 2"),
            ):
                install_provider_into(python, source, wheels, wheels, environment)
            # check_output delegates the read-only metadata probe to run. No
            # pip/build process or target mutation may follow that observation.
            effects.assert_called_once()
            self.assertEqual(effects.call_args.args[0][:4], [str(python), "-I", "-B", "-c"])
            self.assertEqual((metadata / "METADATA").read_bytes(), original)
            self.assertFalse((environment / "dependency-inputs.json").exists())

    def test_missing_or_invalid_build_identity_failed_build_and_mismatches_preserve_prior_record(self):
        for form in (
            "missing",
            "link",
            "fifo",
            "nul",
            "trailing",
            "oversized",
            "malformed",
            "failed",
            "unconfirmed",
            "inspect",
            "label",
        ):
            with self.subTest(form=form), ImageSource() as fixture:
                directory = fixture.repository / ".work/provider"
                directory.mkdir()
                record = directory / "image-id"
                previous = b"previous image evidence\n"
                record.write_bytes(previous)
                result = subprocess.run(
                    ["bash", "tests/fixtures/provider_image_build.sh", str(fixture.repository), form],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=5,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(record.read_bytes(), previous)
                calls = (directory / "native-calls").read_text().splitlines()
                if form not in ("inspect", "label"):
                    self.assertNotIn("controller built-image admission", calls)
                    self.assertNotIn("controller image-input admission", calls)
                if form == "failed":
                    self.assertEqual(result.returncode, 7)

    def test_missing_local_base_or_changed_label_never_replaces_image_record(self):
        for failure in ("base", "label"):
            with self.subTest(failure=failure), ImageSource() as fixture:
                directory = fixture.repository / ".work/provider"
                directory.mkdir()
                record = directory / "image-id"
                previous = b"prior immutable image evidence\n"
                record.write_bytes(previous)
                stub = 'container_native() { inference_result="sha256:wrong-label"; return 0; }; require_provider_inputs "sha256:example" "sha256:expected"'
                if failure == "base":
                    stub = 'container_native() { printf "local-base-inspection-only\\n"; return 42; }; build_provider_image'
                result = subprocess.run(
                    [
                        "bash",
                        "-eu",
                        "-c",
                        'repository_root=$1; image_input_directory=; image=PINNED_BASE; engine=(fixture); print_command=false; pending_diagnostic=; inference_diagnostic=; source "$repository_root/tools/provider_image.sh"; '
                        + stub,
                        "image-admission",
                        str(fixture.repository),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(record.read_bytes(), previous)
                self.assertEqual(sorted(path.name for path in directory.iterdir()), ["image-id"])
                if failure == "base":
                    self.assertEqual(result.returncode, 42)
                    self.assertEqual(result.stdout, "local-base-inspection-only\n")

    def test_linked_locked_wheel_is_refused_during_frozen_capture(self):
        with ImageSource() as fixture:
            outside = fixture.root / "outside-wheel"
            original = fixture.pool_wheels["runtime"].read_bytes()
            shutil.move(fixture.pool_wheels["runtime"], outside)
            fixture.pool_wheels["runtime"].symlink_to(outside)
            result, capture = fixture.capture()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Frozen wheel capture failed", result.stderr)
            self.assertEqual(outside.read_bytes(), original)
            self.assertTrue(fixture.pool_wheels["runtime"].is_symlink())
            self.assertFalse((capture / "runtime-wheels" / outside.name).exists())
            self.assertFalse((fixture.repository / ".work/provider/image-id").exists())

    def test_changed_missing_linked_mode_or_untracked_module_refuses_committed_capture(self):
        for change in ("bytes", "missing", "linked", "mode", "untracked", "ignored"):
            with self.subTest(change=change), ImageSource() as fixture:
                target = fixture.repository / "nmrnet_provider/process.py"
                if change == "bytes":
                    target.write_text("PRIVATE changed code")
                elif change == "missing":
                    target.unlink()
                elif change == "linked":
                    target.unlink()
                    target.symlink_to("../PRIVATE_CANARY")
                elif change == "mode":
                    target.chmod(0o755)
                else:
                    (fixture.repository / "nmrnet_provider/extra.py").write_text("PRIVATE extra code")
                    if change == "ignored":
                        (fixture.repository / ".gitignore").write_text("nmrnet_provider/extra.py\n")
                result, _ = fixture.capture()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Cannot capture controller image inputs", result.stderr)

    def test_pip_refuses_missing_or_changed_locked_wheels_before_building_the_controller(self):
        for role in ("build", "runtime"):
            for mutation in ("missing", "changed"):
                with self.subTest(role=role, mutation=mutation), tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "source"
                    (source / "requirements").mkdir(parents=True)
                    wheelhouses = {}
                    for kind in ("build", "runtime"):
                        wheels = Path(directory) / kind
                        wheels.mkdir()
                        candidate = wheel(wheels, "example", "1")
                        lock = source / "requirements" / ("provider-core-" + kind + ".lock")
                        if kind == "runtime":
                            lock = source / "requirements/provider-core.lock"
                        locked(lock, [("example", "1", [candidate])])
                        wheelhouses[kind] = wheels
                    target = wheelhouses[role] / "example-1-py3-none-any.whl"
                    if mutation == "missing":
                        target.unlink()
                    else:
                        target.write_bytes(b"PRIVATE changed wheel")
                    destination = Path(directory) / "installed"
                    with (
                        patch("tools.provider_environment.subprocess.run", wraps=subprocess.run) as calls,
                        self.assertRaises(subprocess.CalledProcessError),
                    ):
                        install_provider(destination, source, wheelhouses["build"], wheelhouses["runtime"])
                    self.assertFalse(any("wheel" in call.args[0] for call in calls.call_args_list))
                    self.assertFalse((destination / "dependency-inputs.json").exists())
