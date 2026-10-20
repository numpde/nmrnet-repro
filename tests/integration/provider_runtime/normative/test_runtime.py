"""Installed controller proof; no scientific inputs, credentials or live service.

Adapts rdkit_runtime/test_runtime.py and provider-core's contract/test_installation.py
at 9c0d9b0. Source/lock packaging, native signing and isolated import origins are
different claims from semantic interpretation, prediction or deployment.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import re
import site
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class ProviderRuntimeTests(unittest.TestCase):
    def test_complete_distribution_inventory_and_native_signing(self):
        # The build owner validates full lock syntax/hashes. Here package/version
        # rows independently qualify the complete installed distribution set.
        expected = sorted(
            (match[1], match[2])
            for line in Path("/opt/provider/provider-core.lock").read_text().splitlines()
            if (match := re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+) \\", line))
        )
        self.assertEqual(len(expected), 11)
        expected.append(("provider-core", "0.1.0"))
        observed = sorted(
            (re.sub(r"[-_.]+", "-", item.metadata["Name"]).lower(), item.version) for item in importlib.metadata.distributions()
        )
        self.assertEqual(sorted(expected), observed)
        key = Ed25519PrivateKey.generate()
        key.public_key().verify(key.sign(b"installed provider controller"), b"installed provider controller")

    def test_isolated_interpreter_loads_only_image_owned_modules(self):
        self.assertEqual(sys.flags.isolated, 1)
        self.assertFalse(site.ENABLE_USER_SITE)
        self.assertNotEqual(sys.base_prefix, sys.prefix)
        installed = Path(sysconfig.get_path("purelib")).resolve()
        for name in (
            "provider_core",
            "nmrnet_provider.process",
            "nmrnet_provider.startup_capture",
            "nmrnet_inference.predict",
            "tools.artifacts",
            "tools.container_guard",
            "tools.provider_proxy",
            "tools.bound_http_proxy",
        ):
            with self.subTest(module=name):
                origin = Path(importlib.import_module(name).__file__).resolve(strict=True)
                self.assertTrue(origin.is_relative_to(installed), str(origin))
        for name in (
            "torch",
            "rdkit",
            "numpy",
            "lmdb",
            "pip",
            "setuptools",
            "tests",
            "packages",
            "repository_checks",
            "nmrnet_provider.worker_main",
        ):
            with self.subTest(absent=name):
                self.assertIsNone(importlib.util.find_spec(name))
        self.assertFalse(any(Path(item).is_relative_to("/workspace") for item in sys.path))

    def test_foreground_help_ignores_current_directory_and_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nmrnet_provider").mkdir()
            (root / "nmrnet_provider/__init__.py").write_text("raise RuntimeError('PRIVATE ambient import')")
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "nmrnet_provider.process",
                    "--help",
                ],
                cwd=root,
                env={**os.environ, "PYTHONPATH": str(root)},
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--config", result.stdout)
            self.assertNotIn("PRIVATE", result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")

    def test_consumed_dependency_inventory_is_retained_in_the_image(self):
        evidence = json.loads(Path("/opt/provider/dependency-inputs.json").read_bytes())
        self.assertEqual(set(evidence), {"build", "runtime"})
        self.assertEqual(len(evidence["build"]["files"]), 1)
        self.assertEqual(len(evidence["runtime"]["files"]), 11)
        self.assertEqual(
            evidence["runtime"]["lock_sha256"], hashlib.sha256(Path("/opt/provider/provider-core.lock").read_bytes()).hexdigest()
        )
