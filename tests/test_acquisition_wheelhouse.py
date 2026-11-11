import os
import subprocess
import tempfile
import unittest
from pathlib import Path

CALLBACK = str(Path(__file__).resolve().parents[1] / "tools/acquisition_wheelhouse.sh")
HARNESS = r"""
source "$1"
exec 3> "$EVENTS"
image=checker
interface=
shared_acquisition=true
offline_grants=() online_grants=() offline_app=() online_app=()
index=0
select_acquisition_interface() { printf 'select\n' >&3; interface=system; }
acquisition_set_route() { printf 'route:%s\n' "$1" >&3; }
acquisition_worker() {
    local kind=$1 result_name=$2 outcome
    printf '%s\n' "$kind" >&3
    IFS=, read -ra outcomes <<< "$OUTCOMES"
    outcome=${outcomes[index]}
    ((index+=1))
    if [[ $outcome == fail:* ]]; then return "${outcome#fail:}"; fi
    printf -v "$result_name" '%s' "$outcome"
    if [[ $kind == online ]]; then printf 'data\n'; fi
}
nmrnet_acquire_wheelhouse
"""


class CallbackTests(unittest.TestCase):
    def run_callback(self, outcomes):
        with tempfile.TemporaryDirectory() as directory:
            event_path = os.path.join(directory, "events")
            process = subprocess.run(
                ["bash", "-c", HARNESS, "bash", CALLBACK],
                env={**os.environ, "OUTCOMES": outcomes, "EVENTS": event_path},
                capture_output=True,
                text=True,
                check=False,
            )
            with open(event_path, encoding="utf-8") as receipt:
                events = receipt.read().splitlines()
        return process, events

    def test_warm_probe_does_not_select_route(self):
        result, events = self.run_callback("0")
        self.assertEqual((result.returncode, events), (0, ["offline"]))

    def test_miss_runs_online_then_rechecks(self):
        result, events = self.run_callback("3,0,0")
        self.assertEqual((result.returncode, result.stdout), (0, "data\n"))
        self.assertEqual(events, ["offline", "select", "route:system", "online", "offline"])

    def test_failed_owner_is_not_a_miss(self):
        result, events = self.run_callback("fail:3")
        self.assertEqual((result.returncode, events), (3, ["offline"]))

    def test_missing_after_acquisition_is_failure(self):
        result, events = self.run_callback("3,0,3")
        self.assertEqual(result.returncode, 1)
        self.assertIn("remain missing", result.stderr)

    def test_worker_failure_is_preserved_without_recheck(self):
        result, events = self.run_callback("3,17")
        self.assertEqual(result.returncode, 17)
        self.assertEqual(events[-1], "online")


if __name__ == "__main__":
    unittest.main()
