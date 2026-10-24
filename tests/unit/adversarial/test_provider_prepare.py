"""Failed or interrupted preparation never advances to later owners."""

from tools.container_guard import require_container

require_container()

import subprocess
import time
import unittest

from tests.unit.provider_prepare_fakes import PreparationFixture


class ProviderPreparationRefusalTests(unittest.TestCase):
    def test_owner_failure_stops_before_later_images_or_model_acquisition(self):
        fixture = PreparationFixture()
        self.addCleanup(fixture.close)
        fixture.environment["TEST_FAIL_PHASE"] = "container.sh:geometry/image/build"
        result = fixture.run("../cache")
        self.assertNotEqual(0, result.returncode)
        self.assertEqual([call[0] for call in fixture.calls()], [
            "wheelhouse.sh:download", "wheelhouse.sh:download provider-core", "inference_image.sh:build",
            "inference_image.sh:import", "wheelhouse.sh:download preparation", "container.sh:geometry/image/build",
        ])
        self.assertNotIn("Preparation steps finished", result.stderr)

    def test_signal_waits_for_current_owners_cleanup_and_stops(self):
        fixture = PreparationFixture()
        self.addCleanup(fixture.close)
        fixture.environment["TEST_WAIT_PHASE"] = "inference_image.sh:build"
        process = subprocess.Popen(
            fixture.command("../cache"), env=fixture.environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            deadline = time.monotonic() + 5
            while not (fixture.root / "ready").exists():
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            process.terminate()
            process.communicate(timeout=8)
            self.assertNotEqual(0, process.returncode)
            self.assertEqual("settled", (fixture.root / "settled").read_text())
            self.assertEqual([call[0] for call in fixture.calls()], [
                "wheelhouse.sh:download", "wheelhouse.sh:download provider-core", "inference_image.sh:build",
            ])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
