import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ProviderUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "checkout"
        tools = self.root / "tools"
        tools.mkdir(parents=True)
        shutil.copyfile(ROOT / "tools/provider_update.sh", tools / "provider_update.sh")
        shutil.copyfile(ROOT / "tools/image_lifecycle.sh", tools / "image_lifecycle.sh")
        self.record = Path(self.temporary.name) / "phases.txt"
        for name in ("container.sh", "provider_deployment.sh", "image_retirement.sh"):
            (tools / name).write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s|%s|%s\\n' \"$(basename -- \"$0\")\" \"$*\" \"$NMRNET_DOCKER_ENDPOINT_INPUT\" >> \"$NMRNET_UPDATE_RECORD\"\n"
                "[[ ${NMRNET_FAIL_PHASE-} != \"${1-}\" ]] || exit 13\n"
            )

    def run_update(self, **changes):
        environment = dict(os.environ, NMRNET_DEPLOYMENT_INPUT="example",
                           NMRNET_DOCKER_ENDPOINT_INPUT="unix:///selected.sock",
                           NMRNET_UPDATE_RECORD=str(self.record))
        environment.update(changes)
        return subprocess.run(["bash", str(self.root / "tools/provider_update.sh")],
                              env=environment, capture_output=True, text=True, timeout=5)

    def test_update_uses_one_selected_endpoint_and_existing_lifecycle_order(self):
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record.read_text().splitlines(), [
            "container.sh|provider/image/build|unix:///selected.sock",
            "provider_deployment.sh|update|unix:///selected.sock",
            "image_retirement.sh||unix:///selected.sock",
        ])

    def test_update_stops_after_a_failed_phase_and_reports_partial_effects(self):
        result = self.run_update(NMRNET_FAIL_PHASE="update")
        self.assertEqual(result.returncode, 13)
        self.assertEqual(self.record.read_text().splitlines(), [
            "container.sh|provider/image/build|unix:///selected.sock",
            "provider_deployment.sh|update|unix:///selected.sock",
        ])
        self.assertIn("Earlier effects may remain", result.stderr)

    def test_update_can_retain_existing_images(self):
        result = self.run_update(NMRNET_RETAIN_PRIOR_INPUT="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record.read_text().splitlines(), [
            "container.sh|provider/image/build|unix:///selected.sock",
            "provider_deployment.sh|update|unix:///selected.sock",
        ])

    def test_image_lease_spans_build_and_install_but_not_retirement(self):
        tools = self.root / "tools"
        for name in ("container.sh", "provider_deployment.sh", "image_retirement.sh"):
            expected = "success" if name == "image_retirement.sh" else "busy"
            (tools / name).write_text(
                '#!/usr/bin/env bash\n'
                'if flock -n -x "$(dirname "$(dirname "$0")")" true; then observed=success; else observed=busy; fi\n'
                f'[[ $observed == {expected} ]]\n'
            )
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_update_refuses_invalid_retention_before_effects(self):
        result = self.run_update(NMRNET_RETAIN_PRIOR_INPUT="yes")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.record.exists())

    def test_update_requires_an_explicit_safe_deployment_name(self):
        result = self.run_update(NMRNET_DEPLOYMENT_INPUT="../other")
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.record.exists())


if __name__ == "__main__":
    unittest.main()
