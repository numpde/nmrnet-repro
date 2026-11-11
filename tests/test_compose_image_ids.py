import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "host/compose_image_ids.py"
IMAGE = "sha256:" + "a" * 64
SERVICES = ("provider", "prediction", "reader", "smiles", "interpreter", "store")


class ComposeImageIdsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "compose.json"

    def write_services(self, services=None):
        if services is None:
            services = {role: {"image": IMAGE} for role in SERVICES}
        self.path.write_text(json.dumps({"services": services}))

    def run_command(self, *arguments):
        return subprocess.run([sys.executable, str(SCRIPT), *arguments, str(self.path)],
                              capture_output=True, text=True, check=False)

    def test_services_prints_the_complete_immutable_mapping(self):
        self.write_services()
        result = self.run_command("services")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [f"{role}\t{IMAGE}" for role in SERVICES])

    def test_shared_runtime_requires_interpreter_and_store_to_match(self):
        self.write_services()
        result = self.run_command("shared-runtime")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, IMAGE + "\n")

        services = {role: {"image": IMAGE} for role in SERVICES}
        services["store"] = {"image": "sha256:" + "b" * 64}
        self.write_services(services)
        result = self.run_command("shared-runtime")
        self.assertEqual(result.returncode, 2)
        self.assertIn("do not share one runtime image", result.stderr)

    def test_incomplete_service_set_or_mutable_image_refuses(self):
        for services in ({"provider": {"image": IMAGE}},
                         {**{role: {"image": IMAGE} for role in SERVICES}, "unexpected": {"image": IMAGE}},
                         {**{role: {"image": IMAGE} for role in SERVICES}, "provider": {"image": "local:latest"}}):
            with self.subTest(services=services):
                self.write_services(services)
                result = self.run_command("services")
                self.assertEqual(result.returncode, 2)
                self.assertIn("Cannot read frozen Compose image IDs", result.stderr)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
