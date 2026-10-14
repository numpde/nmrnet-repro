"""Require the launcher marker without treating it as container attestation."""

import os
import unittest
from unittest.mock import patch

from tools.container_guard import require_container


class ContainerGuardTests(unittest.TestCase):
    def test_missing_or_inexact_marker_rejects_with_launcher_guidance(self):
        for value in (None, "", "0", "true", "01", "1 "):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["NMRNET_IN_CONTAINER"] = value
                with self.assertRaisesRegex(SystemExit, "maintained Make"):
                    require_container()

    def test_exact_launcher_marker_accepts(self):
        with patch.dict(os.environ, {"NMRNET_IN_CONTAINER": "1"}, clear=True):
            self.assertIsNone(require_container())


if __name__ == "__main__":
    unittest.main()
