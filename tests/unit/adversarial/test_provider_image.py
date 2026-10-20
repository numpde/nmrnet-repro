"""Unverified dependency bytes never reach the controller image installer."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.unit.provider_image_ports import ImageSource

from tools.provider_environment import install_provider
from tools.wheelhouse import inventory


class ProviderDependencyAdmissionTests(unittest.TestCase):
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

    def test_linked_wheelhouse_ancestor_refuses_before_any_engine_access(self):
        with ImageSource() as fixture:
            for name in ("container.sh", "docker_client.sh", "inference_native.sh"):
                shutil.copyfile(Path("tools") / name, fixture.repository / "tools" / name)
            outside = fixture.root / "outside-wheels"
            (outside / "cpu").mkdir(parents=True)
            shutil.rmtree(fixture.repository / ".work/wheelhouse")
            (fixture.repository / ".work/wheelhouse").symlink_to(outside, target_is_directory=True)
            result = subprocess.run(
                ["bash", str(fixture.repository / "tools/container.sh"), "--print", "provider/image/build"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn(".work/wheelhouse must be a directory without a symlink", result.stderr)
            self.assertEqual(result.stdout, "")

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

    def test_missing_extra_or_changed_wheels_refuse_before_installation(self):
        for role in ("build", "runtime"):
            for mutation in ("missing", "extra", "changed"):
                with self.subTest(role=role, mutation=mutation), tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / "source"
                    (source / "requirements").mkdir(parents=True)
                    wheelhouses = {}
                    for kind in ("build", "runtime"):
                        wheels = Path(directory) / kind
                        wheels.mkdir()
                        payload = (kind + " authored dependency fixture").encode()
                        (wheels / "example-1-py3-none-any.whl").write_bytes(payload)
                        lock = source / "requirements" / ("provider-core-" + kind + ".lock")
                        if kind == "runtime":
                            lock = source / "requirements/provider-core.lock"
                        lock.write_text("example==1 \\\n    --hash=sha256:" + hashlib.sha256(payload).hexdigest() + "\n")
                        (wheels / ".complete").write_text(json.dumps(inventory(lock, None, wheels)))
                        wheelhouses[kind] = wheels
                    target = wheelhouses[role] / "example-1-py3-none-any.whl"
                    if mutation == "missing":
                        target.unlink()
                    elif mutation == "extra":
                        (wheelhouses[role] / "unexpected.txt").write_text("PRIVATE unapproved artifact")
                    else:
                        target.write_bytes(b"PRIVATE changed wheel")
                    destination = Path(directory) / "installed"
                    with (
                        patch("tools.provider_environment.subprocess.run", side_effect=AssertionError("installer executed")),
                        self.assertRaises(ValueError),
                    ):
                        install_provider(destination, source, wheelhouses["build"], wheelhouses["runtime"])
                    self.assertFalse(destination.exists())
