"""Verify that the silent-default lane cannot pass without discovered checks."""

from tools.container_guard import require_container

require_container()

import unittest
from unittest.mock import patch

from repository_checks.no_defaults import load_checks


class NoDefaultsLaneTests(unittest.TestCase):
    def test_empty_discovery_is_rejected(self):
        loader = unittest.TestLoader()
        with (
            patch.object(loader, "discover", return_value=unittest.TestSuite()),
            self.assertRaisesRegex(ValueError, "discovery found no tests"),
        ):
            load_checks(loader)

    def test_real_discovery_contains_checks(self):
        self.assertGreater(load_checks(unittest.TestLoader()).countTestCases(), 0)
