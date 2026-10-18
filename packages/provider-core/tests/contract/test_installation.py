"""An installed core is independent of scientific and repository packages."""

import importlib.metadata
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class InstallationContractTests(unittest.TestCase):
    def test_runtime_dependencies_load_and_perform_native_signing(self):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import httpx, http_sf; "
                "from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey; "
                "key = Ed25519PrivateKey.generate(); "
                "key.public_key().verify(key.sign(b'contained runtime'), b'contained runtime')",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_wheel_contains_only_the_declared_core(self):
        distribution = importlib.metadata.distribution("provider-core")
        payload = {str(path) for path in distribution.files if ".dist-info/" not in str(path)}
        self.assertEqual(
            payload,
            {
                "provider_core/__init__.py",
                "provider_core/container_guard.py",
                "provider_core/job_input.py",
                "provider_core/signing.py",
                "provider_core/transport.py",
                "provider_core/network.py",
                "provider_core/api.py",
                "provider_core/attempt_response.py",
                "provider_core/attempt_request.py",
                "provider_core/attempt_record.py",
                "provider_core/attempt_files.py",
                "provider_core/attempt_store.py",
                "provider_core/ownership.py",
                "provider_core/publication.py",
                "provider_core/starting.py",
                "provider_core/analysis.py",
                "provider_core/execution.py",
                "provider_core/recovery.py",
                "provider_core/response_json.py",
                "provider_core/upload_response.py",
                "provider_core/uploads.py",
            },
        )
        self.assertEqual(
            set(distribution.requires), {"cryptography==50.0.0", "http-sf==1.2.1", "httpx==0.28.1", "httpcore==1.0.9"}
        )

    def test_core_imports_without_scientific_or_repository_packages(self):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import provider_core.job_input, provider_core.signing, provider_core.transport, provider_core.network, provider_core.api, provider_core.uploads, provider_core.attempt_response, provider_core.response_json, provider_core.attempt_record, provider_core.attempt_store, provider_core.publication, provider_core.starting, provider_core.analysis, provider_core.execution, provider_core.recovery; import sys; "
                "assert not any(n.split('.')[0] in {'tests','packages','repository_checks','tools','rdkit','torch',"
                "'nmrnet_provider','nmrnet_inference'} for n in sys.modules)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_container_marker_rejects_operational_import(self):
        environment = dict(os.environ)
        environment.pop("PROVIDER_CORE_IN_CONTAINER", None)
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", "import provider_core.job_input"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contained launcher", result.stderr)

    def test_runner_rejects_empty_selected_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tests = root / "tests"
            (tests / "unit").mkdir(parents=True)
            for path in (tests / "__init__.py", tests / "unit/__init__.py"):
                path.write_text("")
            shutil.copyfile(Path(__file__).parents[1] / "run_suite.py", tests / "run_suite.py")
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(tests / "run_suite.py"), "unit"],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contains no tests", result.stderr)
