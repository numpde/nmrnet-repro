"""Exercise the released publisher on cached target-compatible inputs offline."""

import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import wheelhouse_acquire


class SharedAcquisitionIntegrationTests(unittest.TestCase):
    def test_source_bound_projection_and_cached_publisher(self):
        pool = Path("/wheelhouse")
        real_select = wheelhouse_acquire.select_wheels
        calls = 0

        def select(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls <= 2:
                raise subprocess.CalledProcessError(1, "simulate incomplete selection")
            return real_select(*args, **kwargs)

        with patch.object(wheelhouse_acquire, "select_wheels", side_effect=select):
            wheelhouse_acquire.acquire(
                [Path("/input/provider-core-native-install.lock")], pool,
                Path("/input/provider-core-assets.in"), Path("/input/wheelhouse-acquirer-assets.in"),
            )
        self.assertEqual(calls, 3)


if __name__ == "__main__":
    unittest.main()
