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
    def test_removed_production_entry_refuses_and_recovery_requires_explicit_inputs(self):
        environment = {"PATH": os.environ["PATH"]}
        for command, message in ((["bash", "tools/provider.sh"], "Run make help"),
                                 (["bash", "tools/provider_recovery.sh", "inspect"], "explicit CONFIG")):
            with self.subTest(command=command):
                result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=5, check=False)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn(message, result.stderr)

    def test_failed_log_delivery_retains_the_stopped_helper_and_original_receipts(self):
        host = HostFixture()
        self.addCleanup(host.close)
        host.environment.update(FAKE_POSTURE_STATE="created", FAKE_POSTURE_RUNNING="false", FAKE_LOGS_STATUS="7")
        result = host.run('''
source tools/provider.sh
provider_host_root=$FAKE_ROOT
provider_host_diagnostic=
mkdir -p "$provider_host_root/components"
declare -A provider_host_ids=()
provider_host_stop_requested=false
provider_host_grants=()
provider_host_argv=(/bin/true)
provider_host_create helper none "$FAKE_IMAGE" provider_host_grants provider_host_argv false
if provider_host_remove helper; then exit 99; fi
[[ ${provider_host_ids[helper]} == "$FAKE_CID" ]]
[[ -d $provider_host_root/components/helper/retirement ]]
printf '%s\n' "$provider_host_diagnostic" >&2
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Docker log delivery failed", result.stderr)
        self.assertIn("fixture stopped-container diagnostic", result.stderr)
        self.assertEqual([row[0] for row in host.commands()], [b"create", b"inspect", b"start", b"stop", b"inspect", b"logs"])

    def test_logging_posture_mismatch_prevents_component_start_and_retains_exact_cleanup(self):
        host = HostFixture()
        self.addCleanup(host.close)
        host.environment.update(FAKE_POSTURE_STATE="created", FAKE_POSTURE_RUNNING="false", FAKE_LOGGING_MISMATCH="yes")
        result = host.run(r"""
source tools/provider.sh
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components"
declare -A provider_host_ids=()
provider_host_stop_requested=false provider_host_diagnostic=
launch_grants=() launch_command=(/usr/local/bin/python -B /fixture/helper.py)
if provider_host_create controller none "$FAKE_IMAGE" launch_grants launch_command false; then exit 97; fi
[[ ${provider_host_ids[controller]} == "$FAKE_CID" && -n $provider_host_diagnostic ]]
provider_host_remove controller
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()], [b"create", b"inspect", b"stop", b"inspect", b"logs", b"rm"])

    def test_recovery_handoff_requires_seven_private_complete_fields_and_exact_helper_retirement(self):
        valid = b"field\0" * 7
        for fault in ("short", "unterminated", "extra", "public", "owned"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "metadata"
                path.write_bytes({"short": valid[:-6], "unterminated": valid[:-1], "extra": valid + b"\0"}.get(fault, valid))
                path.chmod(0o644 if fault == "public" else 0o600)
                result = subprocess.run(
                    [
                        "bash",
                        "-euc",
                        r"""
source tools/provider.sh
declare -A provider_host_ids=()
provider_host_capture_status=0
if [[ $2 == owned ]]; then provider_host_ids[metadata]=uncertain; fi
provider_host_fields=(sentinel)
if provider_host_adopt_plan "$1" 7 metadata; then exit 97; fi
[[ ${provider_host_fields[*]} == sentinel && -n $provider_host_diagnostic ]]
""",
                        "recovery-proof",
                        str(path),
                        fault,
                    ],
                    env=os.environ,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_recovery_renderer_refuses_unknown_operation_before_any_dispatch(self):
        result = subprocess.run(
            [
                "bash",
                "-euc",
                r"""
source tools/provider.sh
provider_host_diagnostic=
provider_host_recovery_mode=inspect
if provider_host_recovery_arguments unknown; then exit 97; fi
[[ -n $provider_host_diagnostic ]]
""",
            ],
            env=os.environ,
            capture_output=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unsignalled_observation_failure_including_exit_143_remains_a_failure(self):
        for status in ("7", "143"):
            with self.subTest(status=status):
                host = HostFixture()
                self.addCleanup(host.close)
                host.environment["FAKE_OBSERVATION_STATUS"] = status
                result = host.run(r"""
source tools/provider.sh
umask 077
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components" "$provider_host_root/components/controller"
declare -A provider_host_ids=([controller]="$FAKE_CID")
provider_host_stop_requested=false provider_host_diagnostic= provider_host_controller_running=true
if provider_host_observe controller liveness; then exit 97; else observed=$?; fi
[[ $observed == 2 && $inference_cancellation_status == 0 && -n $provider_host_diagnostic ]]
[[ $provider_host_stop_requested == false && $provider_host_controller_running == true ]]
[[ ! -e $provider_host_root/components/controller/interrupted-observation.status ]]
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([row[0] for row in host.commands()], [b"inspect"])

    def test_observer_uncertainty_independent_cleanup_failure_drain_or_existing_evidence_cannot_become_a_clean_stop(self):
        for fault in ("unretired", "cleanup", "drain", "preserved"):
            with self.subTest(fault=fault):
                host = HostFixture()
                self.addCleanup(host.close)
                host.environment["FAKE_OBSERVATION_FAULT"] = fault
                result = host.run(r"""
source tools/provider.sh
umask 077
provider_host_root=$FAKE_STATE/install
directory=$provider_host_root/components/controller
mkdir -p -- "$directory"
declare -A provider_host_ids=([controller]="$FAKE_CID")
provider_host_stop_requested=false provider_host_diagnostic= provider_host_controller_running=true
worker_retirement_command() {
    inference_cancellation_status=143 inference_retirement_confirmed=true inference_diagnostic=
    worker_retirement_diagnostic='Original interrupted observation.'
    printf 'original stdout\n' > "$directory/observation.stdout"
    printf 'original stderr\n' > "$directory/observation.stderr"
    case $FAKE_OBSERVATION_FAULT in
        unretired) inference_retirement_confirmed=false ;;
        cleanup) inference_diagnostic='Independent native evidence cleanup failed.'; worker_retirement_diagnostic+=$inference_diagnostic ;;
    esac
    return 2
}
phase=liveness
if [[ $FAKE_OBSERVATION_FAULT == drain ]]; then phase=drain; fi
if [[ $FAKE_OBSERVATION_FAULT == preserved ]]; then printf 'earlier cancellation\n' > "$directory/interrupted-observation.status"; fi
if provider_host_observe controller "$phase"; then exit 97; else observed=$?; fi
[[ $observed == 2 && -n $provider_host_diagnostic && $provider_host_controller_running == true ]]
cmp -s -- "$directory/observation.stdout" <(printf 'original stdout\n')
cmp -s -- "$directory/observation.stderr" <(printf 'original stderr\n')
if [[ $FAKE_OBSERVATION_FAULT == cleanup ]]; then [[ $provider_host_diagnostic == *'Independent native evidence cleanup failed.'* ]]; fi
if [[ $FAKE_OBSERVATION_FAULT == preserved ]]; then
    cmp -s -- "$directory/interrupted-observation.status" <(printf 'earlier cancellation\n')
fi
""")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_mapped_interface_requires_its_own_literal_grant_before_engine_effects(self):
        for value in ("", "bad interface", "lo\n", "interface-name-too-long", "$(touch SENTINEL)"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                sentinel = Path(directory) / "expanded"
                result = subprocess.run(
                    [
                        "bash",
                        "-euc",
                        r"""
source tools/provider.sh
provider_host_root=$1 provider_host_diagnostic=
provider_host_local_peers= provider_host_local_network= provider_host_local_interface=
provider_host_api_mode=mapped
provider_host_fields=('' '' '' '' 'nmr.localhost:10443' '' '')
provider_host_mapped_addresses=nmr.localhost=127.0.0.1
provider_host_mapped_interface=$2 provider_host_external_interface=
provider_host_resolvers=
provider_host_command() { exit 97; }
if provider_host_routes; then exit 98; fi
[[ $provider_host_diagnostic == *'Mapped service routes require an explicit MAPPED_INTERFACE.'* ]]
[[ ! -e $provider_host_root/resolv.conf ]]
""",
                        "mapped-interface-refusal",
                        directory,
                        value.replace("SENTINEL", str(sentinel)),
                    ],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(sentinel.exists())

    def test_posture_mismatch_prevents_start_and_preserves_the_exact_cleanup_handle_for_both_commands(self):
        for interpreter in ("/opt/provider/environment/bin/python", "/usr/local/bin/python"):
            with self.subTest(interpreter=interpreter):
                host = HostFixture()
                self.addCleanup(host.close)
                host.environment.update(FAKE_POSTURE_STATE="running", FAKE_INTERPRETER=interpreter)
                result = host.run(r"""
source tools/provider.sh
umask 077
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components"
declare -A provider_host_ids=()
provider_host_stop_requested=false provider_host_diagnostic=
launch_grants=()
if [[ $FAKE_INTERPRETER == /opt/provider/environment/bin/python ]]; then
    launch_command=("$FAKE_INTERPRETER" -I -B -m nmrnet_provider.launch_capture)
    record_mounts=false
else
    launch_command=("$FAKE_INTERPRETER" -B /fixture/helper.py)
    record_mounts=true
fi
if provider_host_create helper none "$FAKE_IMAGE" launch_grants launch_command "$record_mounts"; then exit 97; fi
[[ ${provider_host_ids[helper]} == "$FAKE_CID" && -n $provider_host_diagnostic ]]
provider_host_remove helper
[[ ${#provider_host_ids[@]} == 0 ]]
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([row[0] for row in host.commands()], [b"create", b"inspect", b"stop", b"inspect", b"logs", b"rm"])

    def test_helper_mount_capture_failure_prevents_start_and_keeps_its_exact_cleanup_handle(self):
        host = HostFixture()
        self.addCleanup(host.close)
        host.environment.update(FAKE_POSTURE_STATE="created", FAKE_POSTURE_RUNNING="false", FAKE_MOUNTS_STATUS="7")
        result = host.run(r"""
source tools/provider.sh
umask 077
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components"
declare -A provider_host_ids=()
provider_host_stop_requested=false provider_host_diagnostic=
launch_grants=() launch_command=(/usr/local/bin/python -B /fixture/helper.py)
if provider_host_create helper none "$FAKE_IMAGE" launch_grants launch_command true; then exit 97; fi
[[ ${provider_host_ids[helper]} == "$FAKE_CID" && -n $provider_host_diagnostic ]]
provider_host_remove helper
[[ ${#provider_host_ids[@]} == 0 ]]
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()], [b"create", b"inspect", b"inspect", b"stop", b"inspect", b"logs", b"rm"])

    def test_failed_create_adopts_an_exact_handle_for_retirement_without_starting_or_retrying(self):
        host = HostFixture()
        self.addCleanup(host.close)
        host.environment["FAKE_CREATE_STATUS"] = "7"
        result = host.run(r"""
source tools/provider.sh
umask 077
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components"
declare -A provider_host_ids=()
provider_host_stop_requested=false provider_host_diagnostic=
launch_grants=() launch_command=(/usr/local/bin/python -B /fixture/helper.py)
if provider_host_create helper none "$FAKE_IMAGE" launch_grants launch_command true; then exit 97; fi
[[ ${provider_host_ids[helper]} == "$FAKE_CID" ]]
provider_host_remove helper
[[ ${#provider_host_ids[@]} == 0 ]]
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()], [b"create", b"stop", b"inspect", b"logs", b"rm"])

    def test_mismatched_create_receipts_hold_uncertain_ownership_without_guessed_cleanup(self):
        host = HostFixture()
        self.addCleanup(host.close)
        host.environment["FAKE_MISMATCH"] = "yes"
        result = host.run(r"""
source tools/provider.sh
umask 077
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components"
declare -A provider_host_ids=()
provider_host_stop_requested=false provider_host_diagnostic=
launch_grants=() launch_command=(/usr/local/bin/python -B /fixture/helper.py)
if provider_host_create helper none "$FAKE_IMAGE" launch_grants launch_command true; then exit 97; fi
[[ -v provider_host_ids[helper] && -z ${provider_host_ids[helper]} ]]
if provider_host_remove helper; then exit 98; fi
[[ -v provider_host_ids[helper] && -n $provider_host_diagnostic ]]
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([row[0] for row in host.commands()], [b"create"])

    def test_mapped_routes_refuse_missing_extra_ambiguous_or_nonliteral_grants_without_engine_calls(self):
        for fault in ("missing", "separator", "extra", "duplicate", "dns", "newline", "interface", "resolver", "unused"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                result = subprocess.run(
                    [
                        "bash",
                        "-euc",
                        r"""
source tools/provider.sh
provider_host_root=$1 provider_host_diagnostic=
provider_host_local_peers= provider_host_local_network= provider_host_local_interface= provider_host_resolvers=
provider_host_api_mode=mapped
provider_host_fields=('' '' '' '' 'nmr.localhost:10443' '' '')
provider_host_mapped_addresses='nmr.localhost=172.17.0.1'
provider_host_mapped_interface=lo provider_host_external_interface=
case $2 in
missing) provider_host_mapped_addresses= ;;
separator) provider_host_mapped_addresses=172.17.0.1; provider_host_fields[4]=172.17.0.1:10443 ;;
extra) provider_host_mapped_addresses+=' other.localhost=127.0.0.1' ;;
duplicate) provider_host_mapped_addresses+=' nmr.localhost=127.0.0.1' ;;
dns) provider_host_mapped_addresses='nmr.localhost=host-gateway' ;;
newline) provider_host_mapped_addresses+=$'\n' ;;
interface) provider_host_mapped_interface= ;;
resolver) provider_host_resolvers=192.0.2.53 ;;
unused) provider_host_api_mode=external; provider_host_mapped_interface=; provider_host_external_interface=wlp1s0; provider_host_resolvers=192.0.2.53 ;;
esac
provider_host_command() { exit 97; }
if provider_host_routes; then exit 98; fi
[[ -n $provider_host_diagnostic ]]
""",
                        "mapped-proof",
                        directory,
                        fault,
                    ],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

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
provider_host_term_attempted=false provider_host_stop_requested=false docker_client_directory=
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
        self.assertEqual([row[0] for row in host.commands()], [b"kill", b"inspect", b"stop", b"inspect", b"logs", b"rm"])

    def test_expired_controller_drain_reports_failure_even_when_emergency_removal_succeeds(self):
        host, result = self.signal_shutdown(stays_running=True)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("did not finish shutdown before the host drain deadline", result.stderr)
        self.assertIn("inspect durable Attempt state before restart", result.stderr)
        commands = [row[0] for row in host.commands()]
        self.assertEqual(commands.count(b"kill"), 1)
        self.assertEqual(commands[-4:], [b"stop", b"inspect", b"logs", b"rm"])

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
