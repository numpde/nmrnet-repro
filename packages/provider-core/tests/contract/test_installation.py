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
    def test_runtime_wheel_contains_only_the_declared_core(self):
        distribution = importlib.metadata.distribution("provider-core")
        payload = {str(path) for path in distribution.files if ".dist-info/" not in str(path)}
        self.assertEqual(
            payload,
            {
                "provider_core/__init__.py",
                "provider_core/container_guard.py",
                "provider_core/job_input.py",
            },
        )
        self.assertEqual(distribution.requires, None)

    def test_core_imports_without_scientific_or_repository_packages(self):
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                "import provider_core.job_input; import sys; "
                "assert not any(n.split('.')[0] in {'tools','rdkit','torch','nmrnet_provider','nmrnet_inference'} for n in sys.modules)",
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
