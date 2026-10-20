"""Invalid or still-owned launch handoffs cannot authorize host effects."""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit.worker_host_fakes import HostFixture


class ProviderHostAdversarialTests(unittest.TestCase):
    def signal_shutdown(self, *, stays_running):
        host = HostFixture()
        self.addCleanup(host.close)
        host.environment.update(FAKE_CONTROLLER_EXIT="1", FAKE_CONTROLLER_STAYS_RUNNING="yes" if stays_running else "no")
        result = host.run(r"""
source tools/provider.sh
source tools/docker_client.sh
umask 077
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components" "$provider_host_root/components/controller"
printf '%s\n' "$FAKE_CID" > "$provider_host_root/components/controller/worker.cid"
declare -A provider_host_ids=([controller]="$FAKE_CID")
provider_host_diagnostic= provider_host_drain_seconds=1 provider_host_controller_running=true provider_host_controller_exit=0
provider_host_native_open=false provider_host_term_attempted=false provider_host_stop_requested=false docker_client_directory=
provider_workers_service() { :; }
exec {provider_host_lock}< "$provider_host_root"
trap 'provider_host_stop_requested=true' TERM
kill -TERM "$BASHPID"
[[ $provider_host_stop_requested == true ]]
provider_host_finish 0
""")
        return host, result

    def test_controller_failure_during_signal_drain_is_a_failed_host_result_after_exact_cleanup(self):
        host, result = self.signal_shutdown(stays_running=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("controller exited unsuccessfully during shutdown", result.stderr)
        self.assertEqual([row[0] for row in host.commands()], [b"kill", b"inspect", b"stop", b"inspect", b"rm"])

    def test_expired_controller_drain_reports_failure_even_when_emergency_removal_succeeds(self):
        host, result = self.signal_shutdown(stays_running=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("did not finish shutdown before the host drain deadline", result.stderr)
        self.assertIn("inspect durable Attempt state before restart", result.stderr)
        commands = [row[0] for row in host.commands()]
        self.assertEqual(commands.count(b"kill"), 1)
        self.assertEqual(commands[-3:], [b"stop", b"inspect", b"rm"])

    def test_failed_exact_retirement_is_retained_without_an_automatic_repeat(self):
        host = HostFixture()
        self.addCleanup(host.close)
        host.environment["FAKE_STOP_STATUS"] = "7"
        result = host.run(r"""
source tools/provider.sh
umask 077
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components" "$provider_host_root/components/api"
printf '%s\n' "$FAKE_CID" > "$provider_host_root/components/api/worker.cid"
declare -A provider_host_ids=([api]="$FAKE_CID")
provider_host_diagnostic=
if provider_host_remove api; then exit 97; fi
if provider_host_remove api; then exit 98; fi
[[ ${provider_host_ids[api]} == "$FAKE_CID" && -n $provider_host_diagnostic ]]
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()], [b"stop"])

    def test_handoff_refuses_missing_fields_trailing_bytes_public_files_or_unsettled_helper(self):
        valid = b"field\0" * 17
        for fault in ("short", "unterminated", "extra", "tail", "public", "failed", "owned"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "handoff"
                content = {"short": valid[:-6], "unterminated": valid[:-1], "extra": valid + b"\0", "tail": valid + b"tail"}.get(
                    fault, valid
                )
                path.write_bytes(content)
                path.chmod(0o644 if fault == "public" else 0o600)
                result = subprocess.run(
                    [
                        "bash",
                        "-euc",
                        r"""
source tools/provider.sh
declare -A provider_host_ids=()
provider_host_capture_status=0
if [[ $2 == failed ]]; then provider_host_capture_status=2; fi
if [[ $2 == owned ]]; then provider_host_ids[capture]=uncertain; fi
provider_host_fields=(sentinel)
if provider_host_adopt_plan "$1"; then exit 97; fi
[[ ${provider_host_fields[*]} == sentinel && -n $provider_host_diagnostic ]]
""",
                        "host-proof",
                        str(path),
                        fault,
                    ],
                    env=os.environ,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_mount_targets_and_overlapping_roots_refuse_before_any_engine_call(self):
        for path in ("/opt/provider/config", "/etc/ssl", "/native", "/routes/api", "/diagnostics", "/tmp", "/a/../b", "/a,b"):
            with self.subTest(path=path):
                result = subprocess.run(
                    [
                        "bash",
                        "-euc",
                        r"""
source tools/provider.sh
if provider_host_target "$1"; then exit 97; fi
if provider_host_disjoint /private /private/child; then exit 98; fi
if provider_host_disjoint /private/child /private; then exit 99; fi
provider_host_target /home/fixture/config
provider_host_disjoint /private/one /private/two
""",
                        "host-proof",
                        path,
                    ],
                    env=os.environ,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
