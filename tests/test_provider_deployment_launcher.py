"""Finite deployment commands preserve literal inputs and discoverable ownership."""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from itertools import product

ROOT = Path(__file__).resolve().parents[1]


class DeploymentLauncherTests(unittest.TestCase):
    def test_reply_loss_occurs_only_after_the_actual_engine_command_finishes(self):
        for native_status in (0, 9):
            with self.subTest(native_status=native_status):
                command = f"printf 'withheld response\\n'; printf 'actual Engine cause\\n' >&2; exit {native_status}"
                result = subprocess.run(["bash", str(ROOT / "tools/compose_reply_loss.sh"), "bash", "-c", command],
                    capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, 7 if native_status == 0 else native_status)
                self.assertEqual(result.stdout, "")
                self.assertIn("actual Engine cause", result.stderr)
                self.assertEqual("deliberately withheld" in result.stderr, native_status == 0)

    def test_absent_old_target_settles_both_requests_without_controlling_its_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
caller_uid=$(id -u) deployment_root=$2 deployment_name=fixture deployment_action=down
operation_root=$2/original operation_generation=cccccccccccccccccccccccccccccccc
deployment_helper_retired=true inference_retirement_confirmed=true inference_cancellation_status=0
old=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
replacement=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
stop_phase=recover-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb-stop-provider
mkdir -m 700 -- "$operation_root" "$operation_root/effects" "$operation_root/start-provider" "$operation_root/$stop_phase"
printf '%s\\0' start-provider 30 "$operation_root/start-provider" start "$old" > "$operation_root/effects/start-provider.intent"
printf '%s\\0' "$stop_phase" 7 "$operation_root/$stop_phase" stop --time 2 "$old" > "$operation_root/effects/$stop_phase.intent"
deployment_bootstrap() { deployment_existing_count=1; }
deployment_inventory() { declare -gA deployment_ids=([provider]="$replacement") deployment_bundles=([provider]=generation-replacement); }
reads=0
deployment_command() {
    [[ $4 == false && $5 == ps && ${!#} == "id=$old" ]] || return 91
    reads=$((reads + 1)); mkdir -m 700 -- "$3"; : > "$3/response.stdout"
}
deployment_recover_runtime "$operation_root"
[[ $reads == 1 && ${deployment_ids[provider]} == "$replacement" ]]
cmp -s "$operation_root/start-provider/reconciled" <(printf 'fenced\\n')
cmp -s "$operation_root/$stop_phase/reconciled" <(printf 'fenced\\n')
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_log_loss_warning_preserves_the_stopped_containers(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
identifier=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
declare -A deployment_ids=([provider]="$identifier") deployment_pending_ids=([start-provider]="$identifier")
deployment_runtime_root=$2 deployment_runtime_prefix=recover-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-
deployment_command() { : > "$deployment_runtime_root/remove-called"; }
if deployment_fence_role provider > /dev/full; then exit 91; fi
[[ ! -e $2/remove-called && ${deployment_ids[provider]} == "$identifier" ]]
declare -A deployment_bundles=([provider]=generation-old) deployment_running=([provider]=false)
declare -A deployment_restarting=([provider]=false) deployment_pids=([provider]=0)
deployment_selected_bundle() { selected_bundle=/fixture/bundles/generation-new; }
deployment_plan_inputs() { :; }
deployment_runtime_directories() { :; }
if deployment_up > /dev/full; then exit 92; fi
[[ ! -e $2/remove-called && ${deployment_ids[provider]} == "$identifier" ]]
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("write error", result.stderr)

    def test_same_direction_stop_reuses_an_acknowledged_stop_but_never_a_snapshot_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            engine = root / "engine.sh"
            engine.write_text('''set -euo pipefail
root=$1; shift
identifier=${!#}
printf '%s\\n' "$1" >> "$root/calls"
case $1 in
    stop) printf '%s\\n' "$identifier" ;;
    inspect) printf '%s|exited|false|0|false|0|false\\n' "$identifier" ;;
    *) exit 91 ;;
esac
''')
            result = self.shell('''
caller_uid=$(id -u) deployment_root=$2 deployment_name=fixture deployment_action=down
operation_root=$2/original operation_generation=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
deployment_helper_retired=true inference_retirement_confirmed=true inference_cancellation_status=0
deployment_diagnostic= inference_diagnostic=
mkdir -m 700 -- "$operation_root" "$operation_root/effects" "$operation_root/stop-provider"
provider=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
printf '%s\\0' stop-provider 7 "$operation_root/stop-provider" stop --time 2 "$provider" > "$operation_root/effects/stop-provider.intent"
engine=(bash "$3" "$2")
inference_native() { shift 3; "$@"; }
deployment_bootstrap() { deployment_existing_count=1; }
deployment_inventory() {
    declare -gA deployment_ids=([provider]="$provider") deployment_bundles=([provider]=generation-original)
    declare -gA deployment_running=([provider]=false) deployment_restarting=([provider]=false)
    declare -gA deployment_pids=([provider]=0) deployment_stop_seconds=([provider]=2)
}
deployment_recover_runtime "$operation_root"
operation_generation=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
deployment_recover_runtime "$operation_root"
[[ $deployment_recovery_observed == true && ! -e $operation_root/stop-provider/reconciled ]]
effects=("$operation_root"/effects/*)
[[ ${#effects[@]} == 2 ]]
''', root, engine)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((root / "calls").read_text().splitlines(), ["stop", "inspect", "inspect"])

    def test_opposing_provider_request_is_fenced_before_any_dependency_teardown(self):
        for kind, failure in product(("start", "stop"), ("none", "stop", "remove", "absence")):
            with self.subTest(kind=kind, failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                engine = root / "engine.sh"
                engine.write_text('''set -euo pipefail
root=$1 failure=$2; shift 2
identifier=${!#}
printf '%s %s\\n' "$1" "$identifier" >> "$root/calls"
case $1 in
    stop) [[ $failure != stop ]] || exit 7; printf '%s\\n' "$identifier" ;;
    inspect) printf '%s|exited|false|0|false|0|false\\n' "$identifier" ;;
    rm) [[ $failure != remove ]] || exit 8; rm -- "$root/present-$identifier" ;;
    ps) identifier=${identifier#id=}
        if [[ -e $root/present-$identifier || $failure == absence ]]; then printf '%s\\n' "$identifier"; fi ;;
    *) exit 91 ;;
esac
''')
                result = self.shell('''
caller_uid=$(id -u) caller_gid=$(id -g) repository_root=$1 deployment_root=$2
deployment_name=fixture operation_root=$2/original kind=$5
operation_generation=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
deployment_helper_retired=true inference_retirement_confirmed=true inference_cancellation_status=0
deployment_diagnostic= inference_diagnostic=
mkdir -m 700 -- "$operation_root" "$operation_root/effects" "$operation_root/$kind-provider"
provider=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
prediction=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
if [[ $kind == start ]]; then deployment_action=down budget=30 arguments=(start "$provider")
else deployment_action=up budget=7 arguments=(stop --time 2 "$provider"); fi
printf '%s\\0' "$kind-provider" "$budget" "$operation_root/$kind-provider" "${arguments[@]}" > "$operation_root/effects/$kind-provider.intent"
: > "$2/present-$provider"; : > "$2/present-$prediction"
engine=(bash "$3" "$2" "$4")
inference_native() { shift 3; "$@"; }
deployment_bootstrap() { deployment_existing_count=2; }
deployment_inventory() {
    declare -gA deployment_ids=([provider]="$provider" [prediction]="$prediction")
    declare -gA deployment_bundles=([provider]=generation-original [prediction]=generation-original)
    declare -gA deployment_running=([provider]=true [prediction]=true)
    declare -gA deployment_restarting=([provider]=false [prediction]=false)
    declare -gA deployment_pids=([provider]=1 [prediction]=2)
    declare -gA deployment_stop_seconds=([provider]=2 [prediction]=2)
}
if deployment_recover_runtime "$operation_root"; then
    [[ $4 == none && ! -e $2/present-$provider && -e $2/present-$prediction ]]
    cmp -s "$operation_root/$kind-provider/reconciled" <(printf 'fenced\\n')
else [[ $4 != none && ! -e $operation_root/$kind-provider/reconciled ]]; fi
''', root, engine, failure, kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = (root / "calls").read_text().splitlines()
                dependency = "stop " + "b" * 64
                if failure == "none":
                    self.assertLess(calls.index("rm " + "a" * 64), calls.index(dependency))
                else:
                    self.assertNotIn(dependency, calls)
                self.assertFalse(any(call.startswith("start ") or call.startswith("create ") for call in calls))

    def test_same_intent_startup_resumes_only_nonrunning_ids_and_retains_the_original_restriction(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
caller_uid=$(id -u) deployment_name=fixture deployment_root=$2 deployment_action=up repository_root=$1
inference_retirement_confirmed=true deployment_helper_retired=true
deployment_begin_operation
parent=$operation_root
cp -- "$parent/intent" "$2/original-intent"
provider=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
mkdir -m 700 -- "$parent/start-provider"
printf '%s\\0' start-provider 30 "$parent/start-provider" start "$provider" > "$parent/effects/start-provider.intent"
starts=0 observations=0
deployment_bootstrap() { deployment_existing_count=7; }
deployment_selected_bundle() { selected_bundle=/fixture/bundles/generation-original; }
deployment_inventory() {
    if [[ $1 == started ]]; then
        for role in "${!deployment_ids[@]}"; do deployment_running[$role]=true; done
        return 0
    fi
    declare -gA deployment_ids=() deployment_bundles=() deployment_running=() deployment_restarting=() deployment_pids=()
    for role in api interpreter store prediction reader smiles provider; do
        deployment_ids[$role]=$role-id deployment_bundles[$role]=generation-original
        deployment_running[$role]=false deployment_restarting[$role]=false deployment_pids[$role]=0
    done
    deployment_ids[provider]=$provider deployment_running[api]=true deployment_pids[api]=1
}
deployment_plan_inputs() { deployment_startup_seconds=200; [[ $1 == false ]]; }
deployment_runtime_directories() { :; }
deployment_ready() { observations=$((observations + 1)); }
deployment_command() {
    [[ $1 == resume-*-start-* && $3 == "$parent/$1" && $4 == true && $5 == start && $6 != api-id ]] || return 91
    starts=$((starts + 1))
}
deployment_begin_operation
[[ $deployment_recovery_observed == true && $starts == 6 && $observations == 7 && ! -e $parent/complete ]]
cmp -s "$parent/intent" "$2/original-intent"
[[ -f $parent/effects/start-provider.intent && ! -e $parent/start-provider/reconciled ]]
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_recovery_witness_closes_its_local_lock_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
source "$1/tools/compose_qualification.sh"
private=$2
compose_witness_recovery_effects() { return 7; }
if compose_witness_recovery; then exit 91; else [[ $? == 7 ]]; fi
flock --nonblock "$private" true
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_lost_role_create_uses_original_bundle_admission_without_repeating_create(self):
        for outcome in ("realized", "absent", "foreign", "changed-command"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                result = self.shell('''
caller_uid=$(id -u) caller_gid=$(id -g) repository_root=$1 deployment_root=$2
deployment_name=fixture deployment_action=up operation_root=$2/mutation
operation_generation=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
deployment_helper_retired=true inference_retirement_confirmed=true inference_cancellation_status=0
mkdir -m 700 -- "$operation_root" "$operation_root/effects" "$operation_root/create-api" "$2/bundles" "$2/bundles/generation-original"
digest=$(printf '%s\\0%s' "$repository_root" "$deployment_name" | sha256sum)
deployment_project=nmrnet-${digest:0:20}
bundle=$2/bundles/generation-original
arguments=(compose --env-file /dev/null --project-name "$deployment_project" --project-directory "$repository_root" --file "$bundle/compose.json" create --no-build --pull never --no-recreate api)
if [[ $3 == changed-command ]]; then arguments[9]=up; fi
printf '%s\\0' create-api 60 "$operation_root/create-api" "${arguments[@]}" > "$operation_root/effects/create-api.intent"
admissions=0
expected_bundle=$bundle
deployment_load_bootstrap() { [[ $1 == "$expected_bundle" ]] || return 91; }
fixture_outcome=$3
deployment_inventory() {
    admissions=$((admissions + 1))
    [[ $fixture_outcome != foreign ]] || return 2
    declare -gA deployment_ids=() deployment_bundles=()
    if [[ $fixture_outcome != absent ]]; then
        deployment_ids[api]=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
        deployment_bundles[api]=generation-original
    fi
}
deployment_command() { printf 'Unexpected Engine control\\n' >&2; return 92; }
if deployment_recover_creates "$operation_root"; then
    [[ $3 == realized && $admissions == 1 && -f $operation_root/create-api/reconciled ]]
else
    [[ $3 != realized && ! -e $operation_root/create-api/reconciled ]]
    if [[ $3 == changed-command ]]; then [[ $admissions == 0 ]]; else [[ $admissions == 1 ]]; fi
fi
''', directory, outcome)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_event_receipts_exclude_application_output_and_preserve_engine_failure(self):
        operation = "a" * 32
        for status in (0, 7):
            with self.subTest(status=status):
                script = f'''printf '%s\\n' 'PRIVATE_APPLICATION_OUTPUT' \
    'INFO event=child_started operation={operation} pid=14' \
    'INFO event=child_retired operation={operation} returncode=-15 reason=owner_lost' \
    'INFO event=child_started operation={operation} pid=15 PRIVATE_TRAILER' >&2
if [[ {status} != 0 ]]; then printf 'Cannot read Docker logs: connection refused\\n' >&2; fi
exit {status}
'''
                result = subprocess.run(["bash", str(ROOT / "tools/compose_runner_events.sh"), "b" * 64,
                    "bash", "-c", script], capture_output=True, text=True, timeout=3)
                self.assertEqual(result.returncode, status, result.stderr)
                self.assertEqual(result.stdout, f"started\t{operation}\t14\nretired\t{operation}\t-15\towner_lost\n")
                self.assertNotIn("PRIVATE", result.stdout)
                self.assertIn("PRIVATE_APPLICATION_OUTPUT", result.stderr)
                if status != 0:
                    self.assertIn("Cannot read Docker logs: connection refused", result.stderr)

    def test_primary_and_cleanup_engine_failures_keep_both_protected_causes(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
caller_uid=$(id -u) operation_root=$2 deployment_diagnostic= inference_diagnostic=
inference_retirement_confirmed=true inference_cancellation_status=0
deployment_primary_failure_receipt= deployment_additional_failure_receipt=
engine=(bash -c 'printf "owned Engine cause\\n" >&2; exit 7' bash)
inference_native() { shift 3; "$@"; }
if deployment_command helper-start 10 "$2/start" false start; then exit 91; fi
if deployment_command helper-stop 10 "$2/stop" false stop; then exit 92; fi
[[ $deployment_primary_failure_receipt == "$2/start/response.stderr" && $deployment_additional_failure_receipt == "$2/stop/response.stderr" ]]
deployment_name=fixture deployment_action=up operation_established=false operation_owner=mutation
docker_client_cleanup() { :; }
if false; then :; else deployment_finish; fi
''', directory)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(f"Protected primary Engine cause: {directory}/start/response.stderr", result.stderr)
            self.assertIn(f"Protected additional Engine cause: {directory}/stop/response.stderr", result.stderr)

    def test_failed_original_request_write_cannot_dispatch_an_engine_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
operation_root=$2 caller_uid=$(id -u)
inference_retirement_confirmed=true deployment_diagnostic= inference_diagnostic=
mkdir -m 700 -- "$2/effects"
inference_native() { : > "$operation_root/engine-called"; return 0; }
printf() { return 28; }
if deployment_command create-helper 30 "$2/create" true create; then exit 91; else observed=$?; fi
[[ $observed == 28 && ! -e $2/engine-called && ! -e $2/effects/create-helper.intent ]]
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_failed_helper_declaration_is_not_published_or_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
caller_uid=$(id -u) caller_gid=$(id -g)
repository_root=$1 operation_root=$2 operation_owner=query
deployment_name=fixture deployment_action=status deployment_helper_retired=true
operation_generation=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
controller_image_id=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
deployment_tool_source=$1/tools/provider_deployment.py
grants=() arguments=(inventory --help)
deployment_command() {
    [[ $1 == fixture-image-defaults ]] || { : > "$operation_root/engine-create-called"; return 92; }
    mkdir -m 700 -- "$3"
    builtin printf '%s\\0' "$controller_image_id" '[]' SIGTERM null '\"/work\"' > "$3/response.stdout"
    builtin printf '\\n' >> "$3/response.stdout"
}
printf() { return 28; }
if deployment_helper fixture grants arguments; then exit 91; else observed=$?; fi
[[ $observed == 28 && ! -e $2/fixture/request && ! -e $2/engine-create-called && $deployment_helper_retired == true ]]
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_query_lock_cannot_be_acquired_through_a_displaced_receipt_directory_inode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            query = root / "operation/query"
            query.mkdir(parents=True, mode=0o700)
            query.parent.chmod(0o700)
            (query / "retired").write_text("retired\n")
            (query / "retired").chmod(0o600)
            base = '''
umask 077
source "$1/tools/provider_deployment.sh"
caller_uid=$(id -u) deployment_name=fixture deployment_root=$2 deployment_action=status
inference_retirement_confirmed=true deployment_helper_retired=true
'''
            owner_program = base + '''
deployment_remove_operation() { printf 'remove-pending\\n'; read -r gate; rm -rf -- "$1"; }
deployment_begin_operation
printf 'owned\\n'
read -r gate
'''
            delayed_program = base + '''
flock() { printf 'opened\\n'; read -r gate; command flock "$@"; }
deployment_begin_operation
'''
            def start(program):
                return subprocess.Popen(["bash", "-euo", "pipefail", "-c", program, "bash", str(ROOT), str(root)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            owner = start(owner_program)
            delayed = None
            try:
                self.assertEqual(owner.stdout.readline(), "remove-pending\n")
                delayed = start(delayed_program)
                self.assertEqual(delayed.stdout.readline(), "opened\n")
                owner.stdin.write("continue\n")
                owner.stdin.flush()
                self.assertEqual(owner.stdout.readline(), "owned\n")
                _, error = delayed.communicate("continue\n", timeout=3)
                self.assertNotEqual(delayed.returncode, 0, error)
                self.assertTrue((query / "intent").is_file())
            finally:
                if delayed is not None and delayed.poll() is None:
                    delayed.kill()
                    delayed.communicate(timeout=3)
                owner.communicate("done\n", timeout=3)
            self.assertEqual(owner.returncode, 0)

    def test_realized_helper_must_match_original_environment_command_and_complete_bind_set(self):
        for change in ("none", "environment", "command", "bind", "extra-bind", "name"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                result = self.shell('''
source "$1/tools/deployment_helper_grants.sh"
caller_uid=$(id -u) caller_gid=$(id -g)
deployment_name=fixture deployment_action=up repository_root=$1 operation_owner=mutation
operation_generation=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
controller_image_id=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
deployment_tool_source=$1/tools/provider_deployment.py
mkdir -m 700 -- "$2/helper"
grants=(--mount "type=bind,src=$2/input.json,dst=/input.json,readonly")
arguments=(inventory --help)
deployment_command() {
    mkdir -m 700 -- "$3"
    printf '%s\\0' "$controller_image_id" '["PATH=/image/bin"]' '\"\"' null '\"/work\"' > "$3/response.stdout"
    printf '\\n' >> "$3/response.stdout"
}
deployment_helper_declare "$2/helper" fixture nmrnet-helper-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-fixture grants arguments 10
mapfile -d '' -t original < "$2/helper/request"
observed=(cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc created false false 0 "${original[@]}")
case $3 in
    environment) observed[14]='["API_KEY=PRIVATE_ENV_CANARY"]' ;;
    command) observed[24]=PRIVATE_COMMAND_CANARY ;;
    bind) observed[33]=/PRIVATE_BIND_CANARY ;;
    extra-bind) observed[26]=3; observed+=(bind /extra /extra true rprivate) ;;
    name) observed[5]=/foreign-helper ;;
    none)
        # Engine mount enumeration order is not grant authority.
        observed=("${observed[@]:0:27}" "${observed[@]:32:5}" "${observed[@]:27:5}") ;;
esac
printf '%s\\0' "${observed[@]}" > "$2/observed"
printf '\\n' >> "$2/observed"
if deployment_helper_compare "$2/helper/request" "$2/observed"; then
    [[ $3 == none && $deployment_admitted_helper_id == cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc ]]
else [[ $3 != none && -z $deployment_admitted_helper_id ]]; fi
# A lost create reply can be recovered only through the original name and the
# same complete grant comparison, without starting the helper again.
fixture_root=$2
deployment_command() {
    [[ $4 == false ]] || return 93
    mkdir -m 700 -- "$3"
    case $5 in
        ps) printf '%s\\n' cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc > "$3/response.stdout" ;;
        inspect) cp -- "$fixture_root/observed" "$3/response.stdout" ;;
        *) return 94 ;;
    esac
}
if deployment_helper_discover "$2/helper" fixture bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb "$2/helper/recovery-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" "$((SECONDS + 10))"; then
    [[ $3 == none && -f $2/helper/realization && $deployment_created_helper_id == cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc ]]
else [[ $3 != none && ! -e $2/helper/realization ]]; fi
''', directory, change)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("PRIVATE_", result.stderr)

    def test_received_identity_survives_a_later_partial_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
caller_uid=$(id -u)
mkdir -m 700 -- "$2/helper" "$2/helper/recovery-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" "$2/helper/recovery-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
mkdir -m 700 -- "$2/helper/recovery-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/admit" "$2/helper/recovery-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/admit"
printf 'complete\\n' > "$2/helper/recovery-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/admit/response.stdout"
printf 'partial\\n' > "$2/helper/recovery-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb/admit/response.stdout"
deployment_helper_compare() {
    deployment_admitted_helper_id=
    if cmp -s "$2" <(printf 'complete\\n'); then
        deployment_admitted_helper_id=cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
    else return 2; fi
}
deployment_realized_helper "$2/helper"
[[ $deployment_created_helper_id == cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc && -f $2/helper/realization ]]
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_only_joined_read_only_retries_replace_the_latest_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
caller_uid=$(id -u) operation_root=$2
inference_retirement_confirmed=true inference_cancellation_status=0
mkdir -m 700 -- "$2/effects" "$2/helper"
for generation in aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb; do
    mkdir -m 700 -- "$2/helper/recovery-$generation"
    printf '%s\\n' "$generation" > "$2/helper/recovery-$generation/observed"
    mkdir -m 700 -- "$2/helper/recovery-$generation/name"
    printf 'bounded Engine cause\\n' > "$2/helper/recovery-$generation/name/response.stderr"
    deployment_primary_failure_receipt=$2/helper/recovery-$generation/name/response.stderr
    deployment_helper_retain_observation "$2/helper" "$2" fixture "$generation" "$2/helper/recovery-$generation"
    [[ $deployment_primary_failure_receipt == "$2/helper/recovery-observation/name/response.stderr" && -f $deployment_primary_failure_receipt ]]
done
[[ $(cat "$2/helper/recovery-observation/observed") == bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb ]]
held=cccccccccccccccccccccccccccccccc
mkdir -m 700 -- "$2/helper/recovery-$held"
: > "$2/effects/fixture-recovery-$held-stop.intent"
if deployment_helper_retain_observation "$2/helper" "$2" fixture "$held" "$2/helper/recovery-$held"; then exit 91; else [[ $? == 1 ]]; fi
deployment_helper_compact_observations "$2/helper" "$2" fixture
[[ -d $2/helper/recovery-$held && ! -e $2/helper/recovery-$held/readonly-settled ]]
shopt -s nullglob
retained=("$2/helper"/recovery-*)
[[ ${#retained[@]} == 2 ]]
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_one_engine_restart_is_observed_under_original_budget_without_another_start(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
deployment_name=fixture deployment_action=up operation_root=$2
declare -A deployment_ids=([api]=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)
declare -A deployment_readiness_seconds=([api]=3)
deployment_start_deadline=$((SECONDS + 3))
polls=0
deployment_command() {
    [[ $4 == false && $5 == inspect ]] || return 92
    polls=$((polls + 1))
    mkdir -m 700 -- "$3"
    if (( polls == 1 )); then
        printf '%s|false|true|starting\\n' "${deployment_ids[api]}" > "$3/response.stdout"
    else printf '%s|true|false|healthy\\n' "${deployment_ids[api]}" > "$3/response.stdout"; fi
}
deployment_process_status() { [[ $polls == 2 ]] || return 93; deployment_process_phase=ready deployment_process_age=0; }
deployment_ready api
[[ $polls == 2 ]]
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_stopped_matching_stack_reuses_admitted_containers_without_noop_creates(self):
        result = self.shell('''
deployment_name=fixture deployment_project=nmrnet-fixture repository_root=/fixture operation_root=/fixture/operation deployment_runtime_effects=false
declare -A deployment_ids=() deployment_bundles=() deployment_running=() deployment_restarting=() deployment_pids=()
for role in api interpreter store prediction reader smiles provider; do
    deployment_ids[$role]=${role}-id deployment_bundles[$role]=generation-existing
    deployment_running[$role]=false deployment_restarting[$role]=false deployment_pids[$role]=0
done
deployment_selected_bundle() { selected_bundle=/fixture/bundles/generation-existing; }
deployment_plan_inputs() { deployment_startup_seconds=200; [[ $1 == false ]]; }
deployment_runtime_directories() { :; }
deployment_ready() { :; }
deployment_command() { [[ $1 == start-* ]] || return 92; printf '%s\\n' "$1"; }
deployment_inventory() {
    [[ $1 == started ]] || return 92
    for role in api interpreter store prediction reader smiles provider; do deployment_running[$role]=true; done
}
deployment_up
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("start-"), 7)
        self.assertNotIn("create-", result.stdout)

    def test_interrupted_helper_is_retired_by_original_id_without_reexecution(self):
        for failure in ("none", "stop", "remove", "absence", "logs"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                engine = root / "engine.sh"
                engine.write_text('''set -euo pipefail
root=$1 failure=$2; shift 2
identifier=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
printf '%s\\n' "$1" >> "$root/engine-calls"
case $1 in
    ps) if [[ -f $root/present ]]; then printf '%s\\n' "$identifier"; fi ;;
    stop) [[ $failure != stop ]] || exit 7 ;;
    inspect) printf '%s exited false 0 false\\n' "$identifier" ;;
    logs) [[ $failure != logs ]] || exit 8 ;;
    rm) [[ $failure != remove ]] || exit 9
        if [[ $failure != absence ]]; then rm -- "$root/present"; fi ;;
    *) exit 91 ;;
esac
''')
                result = self.shell('''
caller_uid=$(id -u) deployment_name=fixture deployment_root=$2 deployment_action=up
inference_retirement_confirmed=true deployment_helper_retired=true inference_cancellation_status=0
deployment_diagnostic= inference_diagnostic=
engine=(bash "$3" "$2" "$4")
inference_native() { shift 3; "$@"; }
deployment_begin_operation
original=$operation_root
mkdir -m 700 -- "$original/inspection" "$original/inspection/create" "$original/inspection/start"
identifier=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
printf '%s' "$identifier" > "$original/inspection/helper.cid"
printf '%s\\n' "$identifier" > "$original/inspection/create/response.stdout"
printf '%s\\0' inspection-create 30 "$original/inspection/create" create > "$original/effects/inspection-create.intent"
printf '%s\\0' inspection-start 30 "$original/inspection/start" start "$identifier" > "$original/effects/inspection-start.intent"
: > "$2/present"
if deployment_begin_operation; then
    [[ $4 == none && ! -e $original/inspection ]]
else
    [[ $4 != none && -f $original/effects/inspection-create.intent && ! -e $original/inspection/retired ]]
fi
''', root, engine, failure)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = (root / "engine-calls").read_text().splitlines()
                self.assertNotIn("create", calls)
                self.assertNotIn("start", calls)
                self.assertEqual(calls.count("stop"), 1)

    def test_cached_docker_health_cannot_accept_an_expired_record_or_borrow_another_role_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
deployment_name=fixture deployment_action=up operation_root=$2
declare -A deployment_ids=([api]=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa)
declare -A deployment_readiness_seconds=([api]=1 [provider]=100)
deployment_start_deadline=$((SECONDS + 100))
deployment_command() {
    [[ $2 -le 1 && $4 == false ]] || return 92
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
printf '%s\\0' pending-create 30 "$operation_root/pending/create" create > "$operation_root/effects/pending-create.intent"
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

    def test_known_completed_effects_allow_recovery_without_deleting_receipts_by_hand(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
caller_uid=$(id -u) deployment_name=fixture deployment_root=$2 deployment_action=up
inference_retirement_confirmed=true deployment_helper_retired=true
deployment_begin_operation
first_generation=$operation_generation
mkdir -m 700 -- "$operation_root/start-provider"
printf '%s\\0' start-provider 30 "$operation_root/start-provider" start aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa > "$operation_root/effects/start-provider.intent"
printf '0\\n' > "$operation_root/start-provider/response.status"
deployment_begin_operation
[[ $operation_generation != "$first_generation" && ! -e $operation_root/start-provider ]]
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_mutation_reply_remains_held_even_when_next_action_is_down(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.shell('''
caller_uid=$(id -u) deployment_name=fixture deployment_root=$2 deployment_action=up
inference_retirement_confirmed=true deployment_helper_retired=true
deployment_begin_operation
printf '%s\\0' start-provider 30 "$operation_root/start-provider" start aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa > "$operation_root/effects/start-provider.intent"
deployment_action=down
deployment_bootstrap() { return 2; } # No target grants have been admitted in this refusal fixture.
if deployment_begin_operation; then exit 91; fi
[[ -f $operation_root/effects/start-provider.intent ]]
''', directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("unresolved effects", result.stderr)

    def test_acknowledged_helper_create_and_start_do_not_establish_retirement(self):
        for started in (False, True):
            with self.subTest(started=started), tempfile.TemporaryDirectory() as directory:
                result = self.shell('''
caller_uid=$(id -u) deployment_name=fixture deployment_root=$2 deployment_action=up
inference_retirement_confirmed=true deployment_helper_retired=true
deployment_begin_operation
for phase in $3; do
    mkdir -p -m 700 -- "$operation_root/inspection/$phase"
    printf '%s\\0' "inspection-$phase" 30 "$operation_root/inspection/$phase" "$phase" > "$operation_root/effects/inspection-$phase.intent"
    printf '0\\n' > "$operation_root/inspection/$phase/response.status"
done
if deployment_begin_operation; then exit 91; fi
[[ -f $operation_root/effects/inspection-create.intent ]]
''', directory, "create start" if started else "create")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("unresolved effects", result.stderr)

    def shell(self, program, *arguments):
        return subprocess.run(["bash", "-euo", "pipefail", "-c", 'umask 077\nsource "$1/tools/provider_deployment.sh"\n' + program,
                               "bash", str(ROOT), *(str(value) for value in arguments)],
                              capture_output=True, text=True, timeout=5)
