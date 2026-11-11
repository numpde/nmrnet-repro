"""Exercise warm and missing transitions without starting an engine or network."""

from tools.container_guard import require_container

require_container()

import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path.cwd()


class WheelhouseLauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for directory in ("tools", "host", "requirements", "state", "pool"):
            (self.root / directory).mkdir()
        for name in ("wheelhouse.sh", "wheelhouse_path.sh", "acquisition_interface.sh", "operator_guidance.sh", "inference_native.sh", "acquisition_wheelhouse.sh"):
            shutil.copyfile(ROOT / "tools" / name, self.root / "tools" / name)
        for name in ("inference.lock", "preparation.lock", "unicore-source.json", "provider-core-native-install.lock",
                     "provider-core-install.lock", "wheelhouse-tool-assets.in", "provider-core-assets.in",
                     "wheelhouse-acquirer-assets.in", "inference.in"):
            (self.root / "requirements" / name).write_text("fixture\n")
        (self.root / "host/acquisition_bootstrap.py").write_text("# fixture: verification is tested separately\n")
        (self.root / "tools/docker_client.sh").write_text('''docker_client_prepare() { NMRNET_DOCKER_CONTEXT_INPUT=fixture; NMRNET_DOCKER_ENDPOINT_INPUT=unix:///fixture.sock; }
''')
        owner = self.root / "host/bootstrap/acquisition/acquisition_tool"
        owner.mkdir(parents=True)
        (owner / "owner.sh").write_text('''
acquisition_scope() { shift 5; "$@"; }
acquisition_set_route() { :; }
acquisition_worker() {
    local mode=$1 result_name=$2 fixture_image=$3 grants_name=$4 app_name=$5 status
    local -n grants=$grants_name app=$app_name
    printf '%s %s %s %s\\n' "$mode" "$fixture_image" "${grants[*]}" "${app[*]}" >> "$FIXTURE_STATE/events"
    if [[ $mode == offline ]]; then
    count=$(cat "$FIXTURE_STATE/count" 2>/dev/null || printf 0)
    count=$((count + 1))
    printf '%s' "$count" > "$FIXTURE_STATE/count"
    status=$(cat "$FIXTURE_STATE/probe-$count")
    if [[ $status == 99 ]]; then
        trap ': > "$FIXTURE_STATE/probe-done"; exit 143' TERM
        : > "$FIXTURE_STATE/probe-ready"
        while :; do sleep 0.1; done
    fi
    else status=${FIXTURE_ACQUIRE_STATUS:-0}; fi
    printf -v "$result_name" '%s' "$status"
}
''')
        self.env = os.environ | {
            "NMRNET_WHEELHOUSE_INPUT": str(self.root / "pool"),
            "NMRNET_WHEELHOUSE_INPUT_ORIGIN": "command line",
            "FIXTURE_STATE": str(self.root / "state"),
        }
        for name in ("NMRNET_NETWORK_INTERFACE_INPUT", "NMRNET_NETWORK_INTERFACE_INPUT_ORIGIN"):
            self.env.pop(name, None)

    def launch(self, profile="inference", *, statuses=(0,), additions=None):
        for index, status in enumerate(statuses, 1):
            (self.root / "state" / f"probe-{index}").write_text(f"{status}\n")
        return subprocess.run(["bash", str(self.root / "tools/wheelhouse.sh"), "download", profile],
                              env=self.env | (additions or {}), stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=15, check=False)

    def events(self):
        path = self.root / "state/events"
        return path.read_text().splitlines() if path.exists() else []

    def test_warm_profiles_do_not_select_a_route(self):
        for profile in ("inference", "preparation", "provider-core", "provider-support", "shared-tool"):
            with self.subTest(profile=profile):
                (self.root / "state/count").unlink(missing_ok=True)
                (self.root / "state/events").unlink(missing_ok=True)
                result = self.launch(profile)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("already available", result.stdout)
                self.assertEqual(1, len(self.events()))
                self.assertTrue(self.events()[0].startswith("offline "))
                self.assertIn("dst=/wheelhouse,readonly", self.events()[0])

    def test_missing_runs_one_acquisition_and_rechecks(self):
        result = self.launch(statuses=(3, 0), additions={"NMRNET_NETWORK_INTERFACE_INPUT": "lo"})
        self.assertEqual(0, result.returncode, result.stderr)
        events = self.events()
        self.assertEqual(["offline", "online", "offline"], [event.split()[0] for event in events])

    def test_preview_shows_context_route_and_session_without_dispatch(self):
        result = subprocess.run(["bash", str(self.root / "tools/wheelhouse.sh"), "--print", "download", "shared-tool"],
                                env=self.env | {"NMRNET_NETWORK_INTERFACE_INPUT": "lo"},
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("fixture (unix:///fixture.sock)", result.stdout)
        self.assertIn("route: lo", result.stdout)
        self.assertIn("Offline probe:", result.stdout)
        self.assertIn("If missing, acquire:", result.stdout)
        self.assertEqual([], self.events())

    def test_missing_after_acquisition_stops_without_retry(self):
        result = self.launch(statuses=(3, 3), additions={"NMRNET_NETWORK_INTERFACE_INPUT": "lo"})
        self.assertNotEqual(0, result.returncode)
        self.assertIn("remain missing", result.stderr)
        self.assertEqual(3, len(self.events()))

    def test_probe_failure_does_not_dispatch_acquisition(self):
        for status in (1, 2, 130, 143):
            with self.subTest(status=status):
                (self.root / "state/count").unlink(missing_ok=True)
                (self.root / "state/events").unlink(missing_ok=True)
                result = self.launch(statuses=(status,))
                self.assertEqual(status, result.returncode, result.stderr)
                self.assertEqual(1, len(self.events()))

    def test_invalid_explicit_interface_does_not_create_pool(self):
        (self.root / "pool").rmdir()
        result = self.launch(additions={"NMRNET_NETWORK_INTERFACE_INPUT": "not-an-interface"})
        self.assertNotEqual(0, result.returncode)
        self.assertFalse((self.root / "pool").exists())
        self.assertEqual([], self.events())

    def test_signal_waits_for_probe_owner_and_never_dispatches_acquisition(self):
        (self.root / "state/probe-1").write_text("99\n")
        process = subprocess.Popen(["bash", str(self.root / "tools/wheelhouse.sh"), "download", "inference"],
                                   env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not (self.root / "state/probe-ready").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / "state/probe-ready").exists())
            process.send_signal(signal.SIGTERM)
            _stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(143, process.returncode, stderr)
            self.assertTrue((self.root / "state/probe-done").exists())
            self.assertEqual(1, len(self.events()))
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
