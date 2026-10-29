"""Qualify the locked native packages, without reading molecular input files."""

from tools.container_guard import require_container

require_container()

import importlib
import importlib.machinery
import importlib.metadata
import re
import site
import sys
import sysconfig
import unittest
from pathlib import Path

from tools.wheelhouse import requirements


class RDKitRuntimeNormativeTests(unittest.TestCase):
    def test_installed_distributions_match_the_complete_lock(self):
        expected = set(requirements(Path(__file__).with_name("runtime.lock")))
        native_lock = Path(__file__).with_name("native.lock")
        if native_lock.exists():
            expected.update(requirements(native_lock))
        expected = sorted(expected)
        installed = sorted(
            (re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower(), distribution.version)
            for distribution in importlib.metadata.distributions()
        )
        self.assertEqual(expected, installed)

    def test_interpreter_is_isolated_from_base_packages_and_checkout(self):
        self.assertNotEqual(sys.base_prefix, sys.prefix)
        self.assertEqual(1, sys.flags.isolated)
        self.assertFalse(site.ENABLE_USER_SITE)
        for path in sys.path:
            self.assertFalse(Path(path).resolve().is_relative_to("/workspace"))

    def test_all_three_packages_load_native_extensions_from_the_virtual_environment(self):
        installed = Path(sysconfig.get_path("platlib")).resolve()
        for name in ("rdkit.rdBase", "numpy._core._multiarray_umath", "PIL._imaging"):
            with self.subTest(module=name):
                module = importlib.import_module(name)
                origin = Path(module.__file__).resolve(strict=True)
                self.assertTrue(origin.is_relative_to(installed), msg=f"{name} loaded outside the isolated environment")
                self.assertTrue(origin.name.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES)))
