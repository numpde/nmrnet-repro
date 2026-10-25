"""Finite helper failures retain exact handles, receipts and cleanup ownership."""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import unittest

from tests.unit.worker_host_fakes import HostFixture


class ProviderHostAdversarialTests(unittest.TestCase):
    def test_finite_helper_library_refuses_direct_execution(self):
        result = subprocess.run(["bash", "tools/provider.sh"], env={"PATH": os.environ["PATH"]},
                                capture_output=True, text=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Run make help", result.stderr)

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

    def test_noncanonical_paths_and_overlapping_roots_refuse_without_engine_effects(self):
        for path in ("/a/../b", "/a,b"):
            with self.subTest(path=path):
                result = subprocess.run(
                    ["bash", "-euc", r"""
source tools/provider.sh
if provider_host_path "$1"; then exit 97; fi
if provider_host_disjoint /private /private/child; then exit 98; fi
if provider_host_disjoint /private/child /private; then exit 99; fi
provider_host_path /home/fixture/config
provider_host_disjoint /private/one /private/two
""", "host-proof", path], env=os.environ, capture_output=True, timeout=5, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
