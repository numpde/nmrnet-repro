"""Cancellation owns container identity before any acquisition process starts."""

import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile
import time
import unittest


ENGINE_STUB = r'''#!/bin/bash
set -eu
while [[ "$1" != docker ]]; do shift; done
shift 3
operation=$1
shift
printf '%s\n' "$operation $*" >> "$ENGINE_STATE/events"
case "$operation" in
  image) exit 0 ;;
  inspect) printf 'true\n'; exit ;;
  logs) exit ;;
  create|run)
    while [[ "$1" != --cidfile ]]; do shift; done
    receipt=$2
    kind=${receipt##*/}
    kind=${kind%.cid}
    printf '%s\n' "${receipt%/*}" > "$ENGINE_STATE/scratch"
    if [[ "$kind" == "$DELAY_IDENTITY" ]]; then
      : > "$ENGINE_STATE/delaying"
      while [[ ! -f "$ENGINE_STATE/release" ]]; do sleep 0.01; done
    fi
    printf '%s\n' "$kind" > "$receipt"
    if [[ "$kind" == proxy ]]; then
      printf '12345\n' > "${receipt%/*}/ready/port"
    fi
    printf '%s\n' "$kind"
    if [[ "$operation" == run && "$kind" == worker ]]; then
      while [[ ! -f "$ENGINE_STATE/removed-worker" ]]; do sleep 0.01; done
    fi
    ;;
  start)
    kind=${!#}
    : > "$ENGINE_STATE/started-$kind"
    if [[ "$kind" == worker ]]; then
      while [[ ! -f "$ENGINE_STATE/removed-worker" ]]; do sleep 0.01; done
    fi
    ;;
  rm)
    : > "$ENGINE_STATE/removed-${!#}"
    ;;
  *) exit 3 ;;
esac
'''


class NetworkCancellationTests(unittest.TestCase):
    def test_term_during_identity_creation_removes_container_without_starting_it(self):
        for phase in ("proxy", "worker"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary)
                stub = state / "engine.sh"
                stub.write_text(ENGINE_STUB)
                environment = {
                    **os.environ,
                    "NMRNET_WLAN_INTERFACE_INPUT": "lo",
                    "ENGINE_STATE": str(state),
                    "DELAY_IDENTITY": phase,
                    "ENGINE_STUB": str(stub),
                    "BASH_FUNC_env%%": '() { bash "$ENGINE_STUB" "$@"; }',
                }
                process = subprocess.Popen(
                    ["bash", "tools/docker.sh", "run", "--pull", "never", "inert-image"],
                    env=environment, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                try:
                    deadline = time.monotonic() + 5
                    while not (state / "delaying").exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue((state / "delaying").exists())
                    process.send_signal(signal.SIGTERM)
                    time.sleep(0.1)
                    (state / "release").touch()
                    try:
                        _stdout, stderr = process.communicate(timeout=3)
                    except subprocess.TimeoutExpired:
                        self.fail("Cancellation waited for an unidentified running worker instead of removing it")
                    self.assertEqual(process.returncode, 143, stderr)
                    self.assertTrue((state / f"removed-{phase}").exists(), stderr)
                    self.assertFalse((state / f"started-{phase}").exists())
                    scratch = Path((state / "scratch").read_text().strip())
                    self.assertFalse(scratch.exists())
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                    if (state / "scratch").exists():
                        scratch = Path((state / "scratch").read_text().strip())
                        if scratch.exists():
                            shutil.rmtree(scratch)
