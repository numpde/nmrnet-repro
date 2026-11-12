"""Exercise the NMRNet single-worker adapter at the shared-owner boundary."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path.cwd()


class AcquisitionRunTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        tools = self.root / "tools"
        tools.mkdir()
        (self.root / "host").mkdir()
        for name in ("acquisition_run.sh", "acquisition_interface.sh", "inference_native.sh", "operator_guidance.sh"):
            shutil.copyfile(ROOT / "tools" / name, tools / name)
        (tools / "docker_client.sh").write_text(
            "docker_client_prepare() { NMRNET_DOCKER_CONTEXT_INPUT=fixture; NMRNET_DOCKER_ENDPOINT_INPUT=unix:///fixture.sock; }\n"
        )
        owner = self.root / "host/bootstrap/acquisition/acquisition_tool"
        owner.mkdir(parents=True)
        (owner / "source_update.py").write_text("# The SDK tests own verifier mechanics.\n")
        (owner / "owner.sh").write_text("""
acquisition_scope() { shift 5; "$@"; }
acquisition_set_route() { printf 'route:%s\\n' "$1" >> "$EVENTS"; }
acquisition_worker() {
    local mode=$1 output=$2 worker_image=$3 grant_name=$4 app_name=$5
    local -n worker_grants=$grant_name worker_app=$app_name
    printf 'image:%s\\ngrants:%s\\napp:%s\\n' "$worker_image" "${worker_grants[*]}" "${worker_app[*]}" >> "$EVENTS"
    printf 'worker data\\n'
    printf -v "$output" '%s' "${WORKER_STATUS:-0}"
}
""")
        self.events = self.root / "events"
        self.env = {
            name: value for name, value in os.environ.items() if name != "NMRNET_SUPPRESS_NEXT_ACTIONS"
        } | {"NMRNET_NETWORK_INTERFACE_INPUT": "lo", "EVENTS": str(self.events)}

    def run_worker(self, **extra):
        return subprocess.run(
            [
                "bash",
                str(self.root / "tools/acquisition_run.sh"),
                "--success-note",
                "next",
                "--image",
                "fixture-image",
                "--",
                "--read-only",
                "--",
                "tool",
                "--network",
                "argument",
            ],
            env=self.env | extra,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_separates_application_arguments_and_reports_after_success(self):
        result = self.run_worker()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertEqual("worker data\n", result.stdout)
        self.assertIn("next", result.stderr)
        self.assertIn("app:tool --network argument", self.events.read_text())

    def test_worker_failure_preserves_status_without_guidance(self):
        result = self.run_worker(WORKER_STATUS="17")
        self.assertEqual(17, result.returncode)
        self.assertNotIn("next", result.stderr)

    def test_suppressed_guidance_stays_suppressed(self):
        result = self.run_worker(NMRNET_SUPPRESS_NEXT_ACTIONS="1")
        self.assertEqual(0, result.returncode, msg=result.stderr)
        self.assertNotIn("next", result.stderr)

    def test_only_one_suppresses_guidance(self):
        self.assertNotIn("NMRNET_SUPPRESS_NEXT_ACTIONS", self.env)
        self.assertIn("next", self.run_worker().stderr)
        for value in ("", "0", "other"):
            with self.subTest(value=value):
                result = self.run_worker(NMRNET_SUPPRESS_NEXT_ACTIONS=value)
                self.assertEqual(0, result.returncode, msg=result.stderr)
                self.assertIn("next", result.stderr)


if __name__ == "__main__":
    unittest.main()
