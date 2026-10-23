"""Unverified dependency bytes never reach the controller image installer."""

from tools.container_guard import require_container

require_container()

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.unit.provider_image_ports import ImageSource
from tests.unit.wheel_pool_fixtures import locked, wheel
from tests.unit.worker_host_fakes import HostFixture

from tools.provider_environment import install_provider


class ProviderDependencyAdmissionTests(unittest.TestCase):
    def test_native_capture_propagates_failed_git_and_mode_operations_even_after_complete_output(self):
        for operation in ("chmod", "cat-file", "ls-tree"):
            with self.subTest(operation=operation), ImageSource() as source:
                host = HostFixture()
                self.addCleanup(host.close)
                capture = source.root / "native"
                metadata = source.repository / "tools/git_metadata.sh"
                original = metadata.read_text().replace("repository_git() (", "actual_repository_git() (", 1)
                metadata.write_text(
                    original
                    + """
repository_git() {
actual_repository_git "$@" || return $?
for argument; do
    if [[ $argument == "$FAKE_GIT_FAILURE" ]]; then return 91; fi
done
}
"""
                )
                source.git("add", "--", "tools/git_metadata.sh")
                source.git("commit", "--quiet", "-m", "Inject post-output Git failure", "--", "tools/git_metadata.sh")
                host.environment.update(
                    FAKE_REPOSITORY=str(source.repository),
                    FAKE_CAPTURE=str(capture),
                    FAKE_GIT_FAILURE=operation,
                    FAKE_REVISION=source.git("rev-parse", "HEAD").strip(),
                )
                result = host.run(r"""
repository_root=$FAKE_REPOSITORY
chmod() { command chmod "$@" || return $?; if [[ $FAKE_GIT_FAILURE == chmod ]]; then return 91; fi; }
if provider_workers_capture_sources "$FAKE_CAPTURE" "$FAKE_REVISION"; then exit 97; else observed=$?; fi
[[ $observed == 91 ]]
[[ -z $provider_worker_source_root && -z $provider_worker_source_revision ]]
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((host.root / "commands").exists())

    def test_dirty_missing_or_new_native_sources_never_publish_an_admitted_root_or_dispatch_an_engine(self):
        for drift in ("changed", "missing", "new", "missing-committed", "missing-entrypoint", "head-advanced"):
            with self.subTest(drift=drift), ImageSource() as source:
                declared_revision = source.git("rev-parse", "HEAD").strip()
                target = source.repository / "nmrnet_provider/worker_main.py"
                if drift == "changed":
                    target.write_bytes(b"uncommitted change")
                elif drift == "new":
                    (target.parent / "new_worker.py").write_bytes(b"untracked module")
                elif drift == "missing-committed":
                    source.git("rm", "--", "requirements/inference.lock")
                    source.git("commit", "--quiet", "-m", "Remove required input", "--", "requirements/inference.lock")
                elif drift == "missing-entrypoint":
                    source.git("rm", "--", "nmrnet_inference/worker.py")
                    source.git("commit", "--quiet", "-m", "Remove required module", "--", "nmrnet_inference/worker.py")
                elif drift == "head-advanced":
                    unrelated = source.repository / "unrelated.txt"
                    unrelated.write_text("same native bytes, different declared revision")
                    source.git("add", "--", "unrelated.txt")
                    source.git("commit", "--quiet", "-m", "Advance HEAD", "--", "unrelated.txt")
                else:
                    target.unlink()
                if drift != "head-advanced":
                    declared_revision = source.git("rev-parse", "HEAD").strip()
                host = HostFixture()
                self.addCleanup(host.close)
                capture = source.root / "native"
                host.environment.update(
                    FAKE_REPOSITORY=str(source.repository), FAKE_CAPTURE=str(capture), FAKE_REVISION=declared_revision
                )
                result = host.run(r"""
repository_root=$FAKE_REPOSITORY
if provider_workers_capture_sources "$FAKE_CAPTURE" "$FAKE_REVISION"; then exit 97; fi
[[ -z $provider_worker_source_root && -z $provider_worker_source_revision ]]
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse((host.root / "commands").exists())

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
