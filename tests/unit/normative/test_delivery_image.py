"""Delivery builds share frozen-input admission without NMRNet source authority."""

from tools.container_guard import require_container

require_container()

import subprocess
import unittest

from tests.unit.provider_image_ports import ImageSource


class DeliveryImageCommandTests(unittest.TestCase):
    def test_build_target_uses_the_existing_offline_launcher(self):
        result = subprocess.run(["make", "--dry-run", "--no-print-directory", "delivery/image/build"],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "bash tools/container.sh delivery/image/build")

    def test_capture_excludes_scientific_namespaces_and_ignores_unrelated_nmr_edits(self):
        with ImageSource() as fixture:
            result, before = fixture.capture(kind="delivery")
            self.assertEqual(result.returncode, 0, result.stderr)
            paths = (before / "inputs.paths").read_text().splitlines()
            self.assertIn("tools/core_image.sh", paths)
            self.assertIn("tools/core_environment.py", paths)
            self.assertIn("containers/delivery/Dockerfile", paths)
            self.assertFalse(any(path.startswith(("nmrnet_provider/", "nmrnet_inference/")) for path in paths))
            (fixture.repository / "nmrnet_provider/process.py").write_text("Unrelated scientific binding edit\n")
            result, after = fixture.capture(kind="delivery")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((before / "inputs").read_bytes(), (after / "inputs").read_bytes())

    def test_changed_core_input_refuses_capture_before_building(self):
        with ImageSource() as fixture:
            (fixture.repository / "packages/provider-core/src/provider_core/__init__.py").write_text("Changed core bytes\n")
            result, _ = fixture.capture(kind="delivery")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Cannot capture delivery image inputs", result.stderr)
