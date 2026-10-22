#!/usr/bin/env bash
# Fixed synthetic kernel OOM, real allocation/session/retirement proof. The host
# owns Docker; test containers get no engine, credentials, network or model data.
# Profile overrides below belong only to this explicit opt-in qualification.
set -euo pipefail
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ $# != 0 || $(id -u) == 0 || $(id -g) == 0 || $repository_root == *','* || $repository_root == *$'\n'* ]]; then
    printf '%s\n' 'Use make test/integration/provider/prediction-recovery from a nonroot checkout without commas or line breaks.' >&2
    exit 2
fi
umask 077
source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
source "$repository_root/tools/worker_retirement.sh"
source "$repository_root/tools/provider_workers.sh"
source "$repository_root/tools/provider_image.sh"
source "$repository_root/tools/git_inputs.sh"
docker_client_directory= pending_diagnostic= inference_diagnostic=
inference_retirement_confirmed=true inference_cancellation_status=0
print_command=false
scratch= controller_id= root_open=false controller_removed=false finished=false
proof_executions=0

finish() {
    local status=$? token cleanup=0
    trap - EXIT
    if [[ $inference_retirement_confirmed == true && -n $controller_id && $controller_removed == false ]]; then
        if worker_container_retire "$controller_id" "$scratch/controller" "${engine[@]}"; then controller_removed=true
        else cleanup=$?; pending_diagnostic+=$worker_retirement_diagnostic; fi
    fi
    if [[ $inference_retirement_confirmed == true && $root_open == true ]]; then
        provider_workers_admission=closed
        for token in "${!provider_worker_roles[@]}"; do
            if [[ ${provider_worker_retired[$token]} != true ]]; then provider_workers_retire "$token" || cleanup=$?; fi
        done
        pending_diagnostic+=$provider_workers_diagnostic
        if [[ $controller_removed == true ]]; then provider_workers_close || cleanup=$?; fi
    fi
    if [[ $status == 0 && $cleanup != 0 ]]; then status=$cleanup; fi
    if [[ $inference_retirement_confirmed == true ]]; then docker_client_cleanup || status=$?; fi
    if [[ -n $pending_diagnostic ]]; then printf '%s\n' "$pending_diagnostic" >&2; fi
    if [[ $status == 0 && $finished == true ]]; then
        printf '%s\n' 'Synthetic kernel OOM recovery passed: identical requests, one replacement, exact retirement and capacity handoff.'
    fi
    if [[ -n $scratch ]]; then printf 'Private prediction-recovery evidence: %q\n' "$scratch"; fi
    if [[ $status == 0 && $finished != true ]]; then status=2; fi
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker_client_prepare run
require_provider_image
revision=$(repository_git -C "$repository_root" rev-parse HEAD)
inputs=(Makefile tools/prediction_recovery_qualification.sh tests/integration/provider_worker/prediction_recovery_probe.py
    nmrnet_inference/worker.py tools/provider_workers.sh tools/provider_logging.sh tools/worker_retirement.sh tools/inference_native.sh
    tools/docker_client.sh tools/provider_image.sh tools/git_inputs.sh tools/git_metadata.sh)
if [[ -n $(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${inputs[@]}") ]]; then
    pending_diagnostic='Commit prediction-recovery qualification inputs before running their frozen contained proof.'
    exit 2
fi
if [[ -L $repository_root/.work/qualification ]]; then
    pending_diagnostic='Prediction-recovery qualification requires a regular private evidence directory.'; exit 2
fi
mkdir -p -- "$repository_root/.work/qualification"
scratch=$(mktemp -d "$repository_root/.work/qualification/prediction-recovery-XXXXXXXX")
mkdir -m 700 -- "$scratch/sources" "$scratch/output" "$scratch/controller" "$scratch/controller/diagnostics"
copy_git_blobs 'prediction recovery qualification' "$repository_root" "$revision" "$scratch/sources" "$scratch/inputs" "${inputs[@]}"
printf '%s\n' "$revision" > "$scratch/revision"
printf '%s\n' "$provider_image_id" > "$scratch/image-id"
(cd -- "$scratch/sources"; sha256sum -- "${inputs[@]}") > "$scratch/sources.sha256"
provider_workers_open "$scratch/native" 4 1
root_open=true

proof_posture() {
    local directory=$1 identifier=$2 memory_bytes=$3
    provider_workers_command posture "$directory" inspect --type container --format \
        '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.NanoCpus}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{.Config.User}}|{{.HostConfig.RestartPolicy.Name}}|{{.HostConfig.Init}}' "$identifier" || return $?
    if ! cmp -s -- "$directory/posture.stdout" <(printf '%s|%s|created|true|none|false|%s|%s|32|1000000000|["ALL"]|["no-new-privileges:true"]|%s:%s|no|true\n' \
        "$identifier" "$provider_image_id" "$memory_bytes" "$memory_bytes" "$(id -u)" "$(id -g)"); then
        provider_workers_diagnostic='Synthetic recovery component differs from its fixed image, user, isolation or resource profile.'
        return 2
    fi
}

provider_workers_create() {
    local role=$1 token=$2 directory=$provider_workers_root/owned/$2 create_status=0
    [[ $role == prediction && $proof_executions -lt 2 ]] || {
        provider_workers_diagnostic='Synthetic recovery admits only two prediction allocations.'; return 2;
    }
    proof_executions=$((proof_executions + 1))
    provider_worker_ids[$token]= provider_worker_roles[$token]=$role provider_worker_retired[$token]=false
    mkdir -m 700 -- "$directory" "$directory/diagnostics" "$provider_workers_root/ipc/$token" || return $?
    printf 'prediction %s\n' "$token" > "$directory/create-intent" || return $?
    printf '%s\n' "$proof_executions" > "$directory/proof-ordinal" || return $?
    if provider_workers_command create "$directory" create --cidfile "$directory/worker.cid" \
        --init --network none --pull never --read-only --restart no --log-driver none \
        --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus 1 --memory 64m --memory-swap 64m --pids-limit 32 \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=8m \
        --mount "type=bind,src=$provider_workers_root/ipc/$token,dst=/ipc" \
        --mount "type=bind,src=$directory/diagnostics,dst=/diagnostics" \
        --mount "type=bind,src=$scratch/sources/tests/integration/provider_worker/prediction_recovery_probe.py,dst=/proof/probe.py,readonly" \
        --mount "type=bind,src=$scratch/sources/nmrnet_inference/worker.py,dst=/opt/provider/environment/lib/python3.12/site-packages/nmrnet_inference/worker.py,readonly" \
        --entrypoint /bin/bash "$provider_image_id" -eu -c \
        'umask 077; ulimit -f 64; exec /opt/provider/environment/bin/python -I -B /proof/probe.py "$@" > /diagnostics/stdout 2> /diagnostics/stderr' \
        bash worker "$token" "$proof_executions"; then :; else create_status=$?; fi
    worker_container_created "$directory" || { provider_workers_diagnostic+=$worker_retirement_diagnostic; return 2; }
    provider_worker_ids[$token]=$worker_created_id
    [[ $create_status == 0 ]] || return "$create_status"
    proof_posture "$directory" "$worker_created_id" 67108864 || return $?
    if [[ $provider_workers_admission != open ]] || provider_workers_retirement_requested "$token"; then
        provider_workers_retire "$token"
        return $?
    fi
    provider_workers_command start "$directory" start "$worker_created_id" || return $?
    provider_workers_publish "$token" allocated "allocated prediction $token"
}

provider_workers_observe_final_report() {
    local token=$1 directory=$provider_workers_root/owned/$1 identifier=${provider_worker_ids[$1]} name expected_exit=0
    # Observe parent exit before normal retirement can stop it or remove the
    # exact container. Kernel counters and reaped-child facts remain independent.
    provider_workers_command proof-wait "$directory" wait "$identifier" || { pending_diagnostic+=$provider_workers_diagnostic; exit 2; }
    if cmp -s -- "$directory/proof-ordinal" <(printf '1\n'); then expected_exit=137; fi
    if ! cmp -s -- "$directory/proof-wait.stdout" <(printf '%s\n' "$expected_exit"); then
        pending_diagnostic='Synthetic worker did not produce its expected independently observed container exit.'; exit 2
    fi
    provider_workers_command proof-state "$directory" inspect --type container \
        --format '{{.Id}} {{.State.OOMKilled}} {{.State.ExitCode}}' "$identifier" || { pending_diagnostic+=$provider_workers_diagnostic; exit 2; }
    if ! cmp -s -- "$directory/proof-state.stdout" <(printf '%s true %s\n' "$identifier" "$expected_exit") \
        && ! cmp -s -- "$directory/proof-state.stdout" <(printf '%s false %s\n' "$identifier" "$expected_exit"); then
        pending_diagnostic='Synthetic worker engine observation does not match its exact CID and exit receipt.'; exit 2
    fi
    for name in kernel.json requests.json; do
        if [[ ! -f $directory/diagnostics/$name || -L $directory/diagnostics/$name ]] \
            || [[ $(stat -c '%s' -- "$directory/diagnostics/$name") -gt 4096 ]]; then
            pending_diagnostic='Synthetic recovery lacks bounded kernel/request evidence before retirement.'; exit 2
        fi
        ln -- "$directory/diagnostics/$name" "$provider_workers_root/receipts/$token/$name" || return $?
    done
}

controller_create_status=0
if provider_workers_command create "$scratch/controller" create --cidfile "$scratch/controller/worker.cid" \
    --init --network none --pull never --read-only --restart no --log-driver none \
    --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true \
    --cpus 1 --memory 256m --memory-swap 256m --pids-limit 32 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m \
    --mount "type=bind,src=$scratch/sources/tests/integration/provider_worker/prediction_recovery_probe.py,dst=/proof/probe.py,readonly" \
    --mount "type=bind,src=$scratch/output,dst=/output" \
    --mount "type=bind,src=$scratch/controller/diagnostics,dst=/diagnostics" \
    --mount "type=bind,src=$scratch/native/requests,dst=/native/requests" \
    --mount "type=bind,src=$scratch/native/receipts,dst=/native/receipts,readonly" \
    --mount "type=bind,src=$scratch/native/ipc,dst=/native/ipc,readonly" \
    --entrypoint /bin/bash "$provider_image_id" -eu -c \
    'umask 077; ulimit -f 64; exec /opt/provider/environment/bin/python -I -B /proof/probe.py controller > /diagnostics/stdout 2> /diagnostics/stderr'; then :; else controller_create_status=$?; fi
worker_container_created "$scratch/controller" || { pending_diagnostic+=$worker_retirement_diagnostic; exit 2; }
controller_id=$worker_created_id
[[ $controller_create_status == 0 ]] || exit "$controller_create_status"
proof_posture "$scratch/controller" "$controller_id" 268435456 || { pending_diagnostic+=$provider_workers_diagnostic; exit 2; }
provider_workers_command start "$scratch/controller" start "$controller_id"
deadline=$((SECONDS + 90)) last_probe=$SECONDS
while (( SECONDS < deadline )); do
    provider_workers_service || { pending_diagnostic+=$provider_workers_diagnostic; exit 2; }
    if [[ -f $scratch/output/summary.json ]]; then break; fi
    if (( SECONDS >= last_probe + 2 )); then
        last_probe=$SECONDS
        operation=state-$SECONDS
        provider_workers_command "$operation" "$scratch/controller" inspect --type container --format '{{.State.Status}}' "$controller_id"
        if ! cmp -s -- "$scratch/controller/$operation.stdout" <(printf 'running\n'); then
            pending_diagnostic='Synthetic prediction recovery stopped before its complete kernel/retirement summary; inspect private diagnostics.'; exit 2
        fi
    fi
    sleep 0.05
done
[[ -f $scratch/output/summary.json && $proof_executions == 2 ]] || {
    pending_diagnostic='Synthetic prediction recovery exceeded its finite budget or allocation bound.'; exit 2;
}
provider_workers_command wait "$scratch/controller" wait "$controller_id"
cmp -s -- "$scratch/controller/wait.stdout" <(printf '0\n') || {
    pending_diagnostic='Synthetic prediction recovery did not complete its contained assertions.'; exit 2;
}
finished=true
