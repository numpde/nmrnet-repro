"""Finite deployment commands preserve literal inputs and discoverable ownership."""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class DeploymentLauncherTests(unittest.TestCase):
    def test_cached_docker_health_cannot_accept_an_expired_record_or_borrow_another_role_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
deployment_name=fixture deployment_action=up operation_root=$2
declare -A deployment_ids=([api]=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)
declare -A deployment_readiness_seconds=([api]=1 [provider]=100)
deployment_start_deadline=$((SECONDS + 100))
deployment_command() {
    [[ $2 -le 1 && $4 == false ]]
    mkdir -m 700 -- "$3"
    printf '%s|true|false|healthy\\n' "${deployment_ids[api]}" > "$3/response.stdout"
}
deployment_process_status() { deployment_process_phase=ready deployment_process_age=31001; }
if deployment_ready api; then exit 91; fi
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("api readiness was not confirmed within its 1s allowance", result.stderr)

    def test_repeated_up_observes_all_seven_without_lifecycle_dispatch(self):
        result = self.shell('''
deployment_name=fixture
declare -A deployment_ids=() deployment_bundles=() deployment_running=() deployment_restarting=() deployment_pids=()
for role in api interpreter store prediction reader smiles provider; do
    deployment_ids[$role]=${role}-id deployment_bundles[$role]=generation-existing
    deployment_running[$role]=true deployment_restarting[$role]=false deployment_pids[$role]=123
done
deployment_selected_bundle() { selected_bundle=/fixture/bundles/generation-existing; }
deployment_plan_inputs() { deployment_startup_seconds=30; [[ $1 == false ]]; }
deployment_ready() { printf 'observed:%s\\n' "$1"; }
deployment_command() { printf 'unexpected lifecycle command\\n' >&2; return 92; }
deployment_runtime_directories() { return 93; }
deployment_up
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("observed:"), 7)
        self.assertIn("no component was restarted", result.stdout)

    def test_control_handoff_refuses_partial_or_executable_role_before_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "must-not-execute"
            initial, invalid = root / "initial", root / "invalid"
            provider = ["provider", "1" * 64, "generation-first", "sha256:" + "2" * 64, "exited", "false", "false", "0", "30"]
            reader = ["reader", "3" * 64, *provider[2:]]
            initial.write_bytes(b"\0".join(value.encode() for value in provider) + b"\0")
            invalid.write_bytes(b"\0".join(value.encode() for value in [*reader, f"$(touch {sentinel})"]) + b"\0")
            for path in (initial, invalid):
                path.chmod(0o600)
            result = self.shell('''
caller_uid=$(id -u)
deployment_inventory_rows "$2" 9
if deployment_inventory_rows "$3" 9; then exit 91; fi
[[ ${deployment_ids[provider]} == "$4" && ! -v deployment_ids[reader] ]]
''', initial, invalid, "1" * 64)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(sentinel.exists())

    def test_queries_are_discoverable_and_do_not_take_the_mutation_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base = '''
caller_uid=$(id -u)
deployment_name=fixture deployment_root=$2
inference_retirement_confirmed=true deployment_helper_retired=true
'''
            program = 'source "$1/tools/provider_deployment.sh"\n' + base + '''
deployment_action=install
exec 9< "$deployment_root"
flock --nonblock 9
deployment_begin_operation
printf 'owned\\n'
read -r done
'''
            owner = subprocess.Popen(["bash", "-euo", "pipefail", "-c", program, "bash", str(ROOT), str(root)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(owner.stdout.readline(), "owned\n")
                query = self.shell(base + '''
deployment_action=status
deployment_begin_operation
[[ $operation_root == "$deployment_root/operation/query" ]]
[[ -f $deployment_root/operation/mutation/intent && -f $operation_root/intent ]]
''', root)
                self.assertEqual(query.returncode, 0, query.stderr)
                again = self.shell(base + 'deployment_action=status\ndeployment_begin_operation\n', root)
                self.assertNotEqual(again.returncode, 0)
                self.assertIn("operation/query", again.stderr)
            finally:
                owner.communicate("done\n", timeout=3)
            self.assertEqual(owner.returncode, 0)

    def test_missing_and_malicious_names_fail_before_any_engine_call(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "must-not-execute"
            for name in ("", f"web$(touch {sentinel})"):
                result = subprocess.run(["bash", str(ROOT / "tools/provider_deployment.sh"), "up"],
                                        env=dict(os.environ, NMRNET_DEPLOYMENT_INPUT=name),
                                        capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("explicit DEPLOYMENT", result.stderr)
                self.assertNotIn("Docker", result.stderr)
            self.assertFalse(sentinel.exists())

    def shell(self, program, *arguments):
        return subprocess.run(["bash", "-euo", "pipefail", "-c", 'source "$1/tools/provider_deployment.sh"\n' + program,
                               "bash", str(ROOT), *(str(value) for value in arguments)],
                              capture_output=True, text=True, timeout=5)
