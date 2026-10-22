"""Installed-core lanes keep package and consumer authority separate."""

from tools.container_guard import require_container

require_container()

import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class CoreLauncherTests(unittest.TestCase):
    def test_native_science_lanes_use_the_existing_host_qualification_owner(self):
        for lane, scenario in (
            ("smiles", "scientific-smiles"),
            ("pasted", "scientific-pasted"),
            ("uploads", "scientific-upload"),
        ):
            with self.subTest(lane=lane):
                result = subprocess.run(
                    ["make", "--dry-run", "--no-print-directory", "test/integration/provider-scientific-worker/" + lane],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "bash tools/worker_qualification.sh " + scenario)

    def test_upload_reader_lane_uses_the_existing_host_qualification_owner(self):
        result = subprocess.run(
            ["make", "--dry-run", "--no-print-directory", "test/integration/provider-geometry-worker/uploads"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "bash tools/worker_qualification.sh geometry-upload")

    def checkout(self, root):
        for name in (
            "tools",
            "packages/provider-core",
            ".work/wheelhouse/cpu",
            ".work/provider-core/wheelhouse",
            "requirements",
            "nmrnet_provider",
            "nmrnet_inference",
            "tests/integration/provider_core",
            "tests/integration/provider_runtime",
            "tests/fixtures/preparation/normative",
        ):
            (root / name).mkdir(parents=True, exist_ok=True)
        for name in (
            "container.sh",
            "docker_client.sh",
            "inference_native.sh",
            "container_guard.py",
            "artifacts.py",
            "bound_http_proxy.py",
            "provider_proxy.py",
            "container_resources.py",
        ):
            shutil.copyfile(Path("tools") / name, root / "tools" / name)
        for name in (
            "requirements/provider-core-build.lock",
            "requirements/provider-core.lock",
            ".work/wheelhouse/cpu/setuptools-84.0.0-py3-none-any.whl",
            "tests/fixtures/preparation/normative/mixed_records.xyz",
            "tests/fixtures/preparation/expected.json",
            "tests/integration/provider_runtime/interpreter_ambiguity.py",
            "tests/integration/provider_runtime/stage_api_config.py",
            "tests/integration/provider_core/config_ports.py",
            "tests/fixtures/preparation/interpreter_ambiguity.json",
        ):
            (root / name).write_text("fixture")

    def test_each_selection_is_offline_with_only_readonly_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.checkout(root)
            for lane, suite, patterns in (
                ("test/unit/provider-core", "unit", []),
                ("test/unit/provider-core/credential", "unit", ["test_credential.py", "test_signing.py"]),
                ("test/unit/provider-core/hello", "unit", ["test_hello.py"]),
                ("test/unit/provider-core/transport", "unit", ["test_transport.py", "test_network.py"]),
                (
                    "test/unit/provider-core/admission",
                    "unit",
                    ["test_admission_inventory.py", "test_attempt_store.py", "test_attempt_history.py"],
                ),
                (
                    "test/unit/provider-core/uploads",
                    "unit",
                    ["test_upload_acquisition.py", "test_upload_file_acquisition.py", "test_upload_snapshot.py"],
                ),
                ("test/unit/provider-core/diagnostics", "unit", ["test_failure_projection.py"]),
                (
                    "test/unit/provider-core/recovery",
                    "unit",
                    [
                        "test_terminal_record.py",
                        "test_publication.py",
                        "test_recovery.py",
                        "test_controller*.py",
                        "test_failure_projection.py",
                    ],
                ),
                (
                    "test/unit/provider-core/api",
                    "unit",
                    [
                        "test_api*.py",
                        "test_attempt_start.py",
                        "test_attempt_progress.py",
                        "test_attempt_outcome.py",
                        "test_upload_api.py",
                        "test_publication.py",
                    ],
                ),
                ("test/unit/provider-core/normative", "unit/normative", []),
                ("test/unit/provider-core/adversarial", "unit/adversarial", []),
                ("test/contract/provider-core", "contract", []),
                ("test/integration/provider-core", "integration", []),
                ("test/integration/provider-core/chat", "integration/chat", []),
                ("test/integration/provider-core/hello", "integration/hello", []),
                ("test/integration/provider-core/transport", "integration/transport", []),
                ("test/integration/provider-core/api", "integration/api", []),
                ("test/integration/provider-core/config", "integration", ["test_nmrnet_config.py"]),
                ("test/integration/provider-core/runtime", "integration", ["test_nmrnet_runtime.py"]),
                ("test/integration/provider-core/admission", "integration", ["test_nmrnet_service_admission.py"]),
                (
                    "test/integration/provider-core/service",
                    "integration",
                    ["test_nmrnet_service.py", "test_nmrnet_service_recovery.py"],
                ),
                ("test/integration/provider-core/native-factory", "integration", ["test_nmrnet_native_factory.py"]),
                ("test/integration/provider-core/lifecycle", "integration", ["test_nmrnet_*_lifecycle.py"]),
                ("test/integration/provider-core/smiles", "integration", ["test_nmrnet_smiles_*.py"]),
                (
                    "test/integration/provider-core/uploads",
                    "integration",
                    [
                        "test_nmrnet_upload_session.py",
                        "test_upload_preparation.py",
                        "test_upload_acquisition.py",
                        "test_secs_sources.py",
                    ],
                ),
                (
                    "test/unit/provider-core/prepared-identity",
                    "unit",
                    [
                        "test_prepared_identity.py",
                        "test_execution*.py",
                        "test_attempt_transitions.py",
                        "test_terminal_record.py",
                        "test_recovery.py",
                    ],
                ),
                (
                    "test/integration/provider-core/pasted",
                    "integration",
                    ["test_nmrnet_pasted.py"],
                ),
                (
                    "test/integration/provider-core/interpreter",
                    "integration",
                    ["test_nmrnet_interpreter.py", "test_nmrnet_interpreter_preparation.py"],
                ),
                (
                    "test/integration/provider-core/prepared-identity",
                    "integration",
                    [
                        "test_nmrnet_session.py",
                        "test_attempt_store.py",
                        "test_terminal_store.py",
                        "test_magnet_adaptation.py",
                        "test_child_retirement.py",
                        "test_execution.py",
                    ],
                ),
            ):
                with self.subTest(lane=lane):
                    result = subprocess.run(
                        ["bash", str(root / "tools/container.sh"), "--print", lane], capture_output=True, text=True, check=False
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    arguments = shlex.split(result.stdout)
                    expected = [suite, "--pattern", *patterns] if patterns else [suite]
                    self.assertEqual(expected, arguments[-len(expected) :])
                    self.assertEqual(arguments[arguments.index("--network") + 1], "none")
                    self.assertIn("--read-only", arguments)
                    self.assertIn("--init", arguments)
                    mounts = [arguments[i + 1] for i, item in enumerate(arguments) if item == "--mount"]
                    self.assertTrue(all(item.endswith(",readonly") for item in mounts))
                    self.assertNotIn("docker.sock", " ".join(mounts))
                    self.assertTrue(any("dst=/runtime-deps,readonly" in item for item in mounts))
                    self.assertTrue(any("dst=/core-runtime.lock,readonly" in item for item in mounts))
                    self.assertIn("/runtime:rw,nosuid,nodev,exec,size=128m", arguments)
                    self.assertIn("/tmp:rw,nosuid,nodev,noexec,size=64m", arguments)
                    self.assertEqual(any("dst=/consumer/" in item for item in mounts), suite == "integration")
                    fixture_mounts = [item for item in mounts if "dst=/fixtures/" in item]
                    expected_fixture_count = 0
                    if lane == "test/integration/provider-core":
                        expected_fixture_count = 3
                    elif lane == "test/integration/provider-core/uploads":
                        expected_fixture_count = 2
                    elif lane == "test/integration/provider-core/lifecycle":
                        expected_fixture_count = 1
                    self.assertEqual(len(fixture_mounts), expected_fixture_count)

    def test_upload_fixture_symlinks_are_rejected_before_container_dispatch(self):
        for name in ("tests/fixtures/preparation", "tests/fixtures/preparation/normative/mixed_records.xyz"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.checkout(root)
                original = root / name
                original.rename(root / "external-fixture")
                original.symlink_to(root / "external-fixture")
                result = subprocess.run(
                    ["bash", str(root / "tools/container.sh"), "--print", "test/integration/provider-core/uploads"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("fixture", result.stderr)
                self.assertEqual(result.stdout, "")

    def test_aggregate_and_dedicated_probe_tests_receive_the_same_readonly_inputs(self):
        inputs = (
            ("tests/integration/provider_runtime/interpreter_ambiguity.py", "/consumer/interpreter_ambiguity.py"),
            ("tests/integration/provider_runtime/stage_api_config.py", "/consumer/stage_api_config.py"),
            ("tests/integration/provider_core/config_ports.py", "/consumer/config_ports.py"),
            ("tests/fixtures/preparation/interpreter_ambiguity.json", "/fixture/interpreter_ambiguity.json"),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.checkout(root)
            for lane, granted in (
                ("test/integration/provider-core", True),
                ("test/integration/provider-core/interpreter-ambiguity", True),
                ("test/unit/provider-core", False),
            ):
                with self.subTest(lane=lane):
                    result = subprocess.run(
                        ["bash", str(root / "tools/container.sh"), "--print", lane], capture_output=True, text=True, check=False
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    arguments = shlex.split(result.stdout)
                    mounts = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--mount"]
                    for name, destination in inputs:
                        self.assertEqual(f"type=bind,src={root}/{name},dst={destination},readonly" in mounts, granted)
                    self.assertEqual(
                        f"type=bind,src={root}/tools/container_resources.py,dst=/consumer/tools/container_resources.py,readonly"
                        in mounts,
                        lane == "test/integration/provider-core",
                    )
                    self.assertEqual(
                        f"type=bind,src={root}/tests/fixtures/preparation,dst=/fixtures/preparation,readonly" in mounts,
                        lane == "test/integration/provider-core",
                    )
                    self.assertEqual(arguments[arguments.index("--network") + 1], "none")
                    if lane == "test/integration/provider-core":
                        self.assertEqual(arguments[-1], "integration")
                        self.assertNotIn("--pattern", arguments)

    def test_symlinked_build_dependency_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.checkout(root)
            dependency = root / ".work/wheelhouse/cpu/setuptools-84.0.0-py3-none-any.whl"
            dependency.unlink()
            dependency.symlink_to(root / "requirements/provider-core-build.lock")
            result = subprocess.run(
                ["bash", str(root / "tools/container.sh"), "--print", "test/unit/provider-core"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("regular build input", result.stderr)

    def test_symlinked_consumer_test_parent_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.checkout(root)
            (root / "tests/integration").rename(root / "external-tests")
            (root / "tests/integration").symlink_to(root / "external-tests", target_is_directory=True)
            result = subprocess.run(
                ["bash", str(root / "tools/container.sh"), "--print", "test/integration/provider-core"],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("tests/integration", result.stderr)

    def test_symlinked_runtime_inputs_are_rejected(self):
        for name in ("requirements/provider-core.lock", ".work/provider-core", ".work/provider-core/wheelhouse"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.checkout(root)
                original = root / name
                original.rename(root / "external-input")
                original.symlink_to(root / "external-input")
                result = subprocess.run(
                    ["bash", str(root / "tools/container.sh"), "--print", "test/unit/provider-core"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
