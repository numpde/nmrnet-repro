#!/usr/bin/env bash
# One opt-in local transaction through the final production Compose commands.
# TLS/signature and molecular fixtures stay in their existing Python owners.
# This finite caller owns only its disposable peer and qualification grants.

compose_witness_wait() {
    local path=$1 seconds=$2 deadline=$((SECONDS + $2))
    while [[ ! -f $path || -L $path ]]; do
        if (( SECONDS >= deadline )); then
            printf 'Compose qualification did not receive %s within %ss. Inspect bounded Docker logs; no Job was resubmitted.\n' "${path##*/}" "$seconds" >&2
            return 2
        fi
        sleep 0.1
    done
}

compose_witness_native_events() {
    local stage=$1 role identifier path event operation value reason deadline=$((SECONDS + 15))
    local -A starts=() retirements=()
    mkdir -m 700 -- "$scratch/$stage" || return $?
    if [[ $stage == before-restart ]]; then
        inference_native 'observe uninterrupted provider process' 5 stdio "${engine[@]}" ps --all --no-trunc --quiet \
            --filter "label=io.nmrnet-repro.deployment=$deployment" --filter 'label=com.docker.compose.service=provider' \
            > "$scratch/$stage/provider.id" || return $?
        IFS= read -r identifier < "$scratch/$stage/provider.id" || return 2
        [[ $identifier =~ ^[0-9a-f]{64}$ ]] \
            && cmp -s "$scratch/$stage/provider.id" <(printf '%s\n' "$identifier") || return 2
        inference_native 'observe provider restart count' 5 stdio "${engine[@]}" inspect --type container \
            --format '{{.Id}}|{{.RestartCount}}|{{.State.Running}}|{{.State.Restarting}}' "$identifier" \
            > "$scratch/$stage/provider.process" || return $?
        cmp -s "$scratch/$stage/provider.process" <(printf '%s|0|true|false\n' "$identifier") || return 2
    fi
    for role in prediction reader smiles; do
        identifier=
        inference_native 'observe qualification native role' 5 stdio "${engine[@]}" ps --all --no-trunc --quiet \
            --filter "label=io.nmrnet-repro.deployment=$deployment" --filter "label=com.docker.compose.service=$role" \
            > "$scratch/$stage/$role.id" || return $?
        IFS= read -r identifier < "$scratch/$stage/$role.id" || return 2
        [[ $identifier =~ ^[0-9a-f]{64}$ ]] \
            && cmp -s "$scratch/$stage/$role.id" <(printf '%s\n' "$identifier") || return 2
        path=$scratch/$stage/$role.events
        while true; do
            inference_native 'observe bounded native lifecycle fields' 5 stdio \
                bash "$capture/tools/compose_runner_events.sh" "$identifier" "${engine[@]}" > "$path" || return $?
            starts=() retirements=()
            while IFS=$'\t' read -r event operation value reason; do
                case $event in
                    started) [[ ! -v starts[$operation] ]] || return 2; starts[$operation]=$value ;;
                    retired) [[ ! -v retirements[$operation] ]] || return 2; retirements[$operation]=$reason ;;
                    *) return 2 ;;
                esac
            done < "$path"
            if [[ $role == smiles ]]; then [[ ${#starts[@]} == 0 && ${#retirements[@]} == 0 ]] || return 2; break; fi
            (( ${#starts[@]} <= 2 && ${#retirements[@]} <= 2 )) || return 2
            if (( ${#starts[@]} == 2 && ${#retirements[@]} == 2 )); then
                for operation in "${!starts[@]}"; do [[ -v retirements[$operation] ]] || return 2; done
                break
            fi
            (( SECONDS < deadline )) || { printf '%s did not expose two fresh retired children within the qualification allowance.\n' "$role" >&2; return 2; }
            sleep 0.1
        done
        if [[ $stage == after-restart ]]; then
            cmp -s "$path" "$scratch/before-restart/$role.events" \
                && cmp -s "$scratch/$stage/$role.id" "$scratch/before-restart/$role.id" || return 2
        fi
    done
}

compose_witness_recovery() {
    local lock status=0
    exec {lock}< "$private" || return $?
    if flock --nonblock "$lock"; then
        if compose_witness_recovery_effects; then :; else status=$?; fi
    else status=$?; fi
    if (( status != 0 )) && [[ ${inference_retirement_confirmed-false} == true \
        && -d $private/operation/mutation && ! -L $private/operation/mutation ]]; then
        if ! cp -a -- "$private/operation/mutation" "$scratch/recovery-failed-receipts"; then
            qualification_cleanup_held=true
        fi
    fi
    # Releasing the finite caller's lock does not settle Engine effects. The
    # original operation receipts remain the next command's recovery barrier.
    exec {lock}<&- || return $?
    return "$status"
}

compose_witness_recovery_effects() {
    source "$repository_root/tools/provider_deployment.sh"
    local caller_uid=$(id -u) caller_gid=$(id -g) digest identifier
    local deployment_name=$deployment deployment_action=up deployment_root=$private
    local operation_root= operation_owner=unestablished operation_established=false
    local deployment_helper_retired=true deployment_diagnostic=
    local deployment_primary_failure_receipt= deployment_additional_failure_receipt=
    local -a original_engine=("${engine[@]}")
    deployment_begin_operation || return $?
    deployment_selected_bundle || return $?
    deployment_load_bootstrap "$selected_bundle" || return $?
    digest=$(printf '%s\0%s' "$repository_root" "$deployment_name" | sha256sum) || return $?
    deployment_project=nmrnet-${digest:0:20}
    deployment_plan_inputs true || return $?
    deployment_runtime_directories || return $?
    deployment_create_arguments "$selected_bundle" api
    engine=(bash "$capture/tools/compose_reply_loss.sh" "${original_engine[@]}")
    if deployment_command create-api 60 "$operation_root/create-api" true "${deployment_role_create_arguments[@]}"; then
        engine=("${original_engine[@]}")
        printf 'Create-reply loss was not exercised.\n' >&2
        return 2
    fi
    engine=("${original_engine[@]}")
    cmp -s "$operation_root/create-api/response.status" <(printf '7\n') \
        && [[ ! -s $operation_root/create-api/response.stdout ]] || return 2
    deployment_recover_creates "$operation_root" || return $?
    [[ ${#deployment_ids[@]} == 1 && ${deployment_states[api]} == created \
        && ${deployment_running[api]} == false && ${deployment_pids[api]} == 0 ]] || return 2
    identifier=${deployment_ids[api]}
    cp -a -- "$operation_root" "$scratch/lost-create-receipts" || return $?
    # Exercise the public command owner's next-operation admission. Its recovery
    # proof, retained above, must allow progress without another runtime create.
    deployment_begin_operation || return $?
    deployment_inventory recovered || return $?
    [[ ${#deployment_ids[@]} == 1 && ${deployment_ids[api]} == "$identifier" \
        && ${deployment_states[api]} == created && ${deployment_running[api]} == false ]] || return 2
    printf 'Lost create reply recovered original API container %s without starting it.\n' "$identifier"
    compose_witness_late_requests "$identifier"
}

compose_witness_late_requests() {
    local old_identifier=$1 replacement_identifier kind budget grace=${deployment_stop_seconds[api]}
    local -a arguments=()
    for kind in start stop; do
        if [[ $kind == start ]]; then budget=30; arguments=(start "$old_identifier")
        else budget=$((grace + 5)); arguments=(stop --time "$grace" "$old_identifier"); fi
        engine=(bash "$capture/tools/compose_reply_loss.sh" "${original_engine[@]}")
        if deployment_command "$kind-api" "$budget" "$operation_root/$kind-api" true "${arguments[@]}"; then
            engine=("${original_engine[@]}")
            printf 'Engine %s reply loss was not exercised.\n' "$kind" >&2
            return 2
        fi
        engine=("${original_engine[@]}")
        [[ $deployment_command_status == 7 && $inference_retirement_confirmed == true && $inference_cancellation_status == 0 ]] || return 2
    done
    deployment_action=down
    deployment_recover_runtime "$operation_root" || return $?
    [[ ! -v deployment_ids[api] ]] || return 2
    cp -a -- "$operation_root" "$scratch/late-request-receipts" || return $?
    deployment_action=up
    deployment_begin_operation || return $?
    deployment_create_arguments "$selected_bundle" api
    deployment_command create-api 60 "$operation_root/create-api" true "${deployment_role_create_arguments[@]}" || return $?
    deployment_inventory replacement || return $?
    [[ ${#deployment_ids[@]} == 1 && ${deployment_ids[api]} != "$old_identifier" ]] || return 2
    replacement_identifier=${deployment_ids[api]}
    deployment_plan_inputs false || return $?
    deployment_start_role api || return $?
    deployment_start_deadline=$((SECONDS + deployment_startup_seconds))
    deployment_ready api || return $?
    compose_witness_old_id_rejected "$old_identifier" "$grace" || return $?
    deployment_process_status api replacement-survived 5 || return $?
    [[ ( $deployment_process_phase == ready || $deployment_process_phase == working ) && $deployment_process_age -le 30000 ]] || return 2
    printf 'Late start/stop of old API ID %s were rejected; replacement %s remains ready. No Job or model call was issued.\n' "$old_identifier" "$replacement_identifier"
    compose_witness_resume_startup "$replacement_identifier"
}

compose_witness_resume_startup() {
    local identifier=$1 role phase=resume-$operation_generation-start-api
    for role in interpreter store prediction reader smiles provider; do
        deployment_create_arguments "$selected_bundle" "$role"
        deployment_command "create-$role" 60 "$operation_root/create-$role" true "${deployment_role_create_arguments[@]}" || return $?
    done
    engine=(bash "$capture/tools/compose_reply_loss.sh" "${original_engine[@]}")
    if deployment_command "$phase" 30 "$operation_root/$phase" true start "$identifier"; then
        engine=("${original_engine[@]}")
        printf 'Interrupted startup reply loss was not exercised.\n' >&2
        return 2
    fi
    engine=("${original_engine[@]}")
    [[ $deployment_command_status == 7 && $inference_retirement_confirmed == true && $inference_cancellation_status == 0 ]] || return 2
    cp -- "$operation_root/intent" "$scratch/resumed-original.intent" || return $?
    deployment_begin_operation || return $?
    [[ $deployment_recovery_observed == true && ${#deployment_ids[@]} == 7 && ${deployment_ids[api]} == "$identifier" \
        && ! -e $operation_root/complete && ! -e $operation_root/$phase/reconciled ]] || return 2
    cmp -s "$operation_root/intent" "$scratch/resumed-original.intent" || return $?
    compose_witness_reader_crash || return $?
    cp -a -- "$operation_root" "$scratch/resumed-startup-receipts" || return $?
    printf 'Startup recovery confirmed all seven roles ready; the original late-start restriction remains retained.\n'
}

compose_witness_reader_crash() {
    local identifier=${deployment_ids[reader]} image=${deployment_images[reader]} operation observed count budget
    local deployment_start_deadline
    local probe_seconds=15
    local probe_source
    local -a grants=() arguments=()
    deployment_command crash-container 10 "$operation_root/crash-container" false inspect --type container "$identifier" || return $?
    deployment_command crash-image 10 "$operation_root/crash-image" false image inspect "$image" || return $?
    mkdir -m 700 -- "$scratch/crash-admission" "$scratch/reader-crash" || return $?
    grants=(--mount "type=bind,src=$selected_bundle/compose.json,dst=/plan/compose.json,readonly"
        --mount "type=bind,src=$operation_root/crash-container/response.stdout,dst=/evidence/container.json,readonly"
        --mount "type=bind,src=$operation_root/crash-image/response.stdout,dst=/evidence/image.json,readonly"
        --mount "type=bind,src=$scratch/crash-admission,dst=/output")
    arguments=(--plan /plan/compose.json --inspection /evidence/container.json --images /evidence/image.json
        --checkout "$repository_root" --deployment "$deployment_name" --container-id "$identifier" --output /output/receipt.json)
    local deployment_tool_source=$capture/tests/integration/provider_runtime/runner_crash_admission.py
    deployment_helper crash-admission grants arguments || return $?
    deployment_command crash-before 10 "$operation_root/crash-before" false inspect --type container \
        --format '{{.Id}}|{{.RestartCount}}' "$identifier" || return $?
    cmp -s "$operation_root/crash-before/response.stdout" <(printf '%s|0\n' "$identifier") || return 2
    deployment_start_deadline=$((SECONDS + 60))
    : > "$peer_data/reader-crash-start"
    compose_witness_wait "$peer_data/reader-crash-operation" 15 || return $?
    IFS= read -r operation < "$peer_data/reader-crash-operation" || return 2
    [[ $operation =~ ^[0-9a-f]{32}$ ]] \
        && cmp -s "$peer_data/reader-crash-operation" <(printf '%s\n' "$operation") || return 2
    probe_source=$(< "$capture/tests/integration/provider_runtime/runner_crash_probe.py")
    # Killing init's main child may kill this exec probe as well. Its exit code
    # cannot establish old-child retirement; the external pidfd observer does.
    (( deployment_start_deadline - SECONDS >= probe_seconds + 5 )) || return 2
    compose_witness_crash_execute "$identifier" "$operation" "$probe_source" "$probe_seconds" || return $?
    budget=$((deployment_start_deadline - SECONDS)); (( budget > 0 )) || return 2
    if (( budget > 15 )); then budget=15; fi
    compose_witness_wait "$peer_data/reader-old-child-exited.json" "$budget" || return $?
    deployment_ready reader || return $?
    budget=$((deployment_start_deadline - SECONDS)); (( budget > 0 )) || return 2
    if (( budget > 5 )); then budget=5; fi
    deployment_command crash-restarted "$budget" "$operation_root/crash-restarted" false inspect --type container \
        --format '{{.Id}}|{{.RestartCount}}' "$identifier" || return $?
    IFS='|' read -r observed count < "$operation_root/crash-restarted/response.stdout" || return 2
    [[ $observed == "$identifier" && $count =~ ^[1-9][0-9]*$ ]] \
        && cmp -s "$operation_root/crash-restarted/response.stdout" <(printf '%s|%s\n' "$identifier" "$count") || return 2
    budget=$((deployment_start_deadline - SECONDS)); (( budget > 0 )) || return 2
    if (( budget > 5 )); then budget=5; fi
    deployment_command crash-fresh-root "$budget" "$operation_root/crash-fresh-root" false exec "$identifier" \
        /opt/rdkit/environment/bin/python -I -B -c 'import os; assert not os.listdir("/scratch"), "Restarted reader retained old scratch"; print("fresh-root")' || return $?
    cmp -s "$operation_root/crash-fresh-root/response.stdout" <(printf 'fresh-root\n') || return 2
    printf 'fresh-root\n' > "$peer_data/reader-fresh-root.pending" || return $?
    mv -- "$peer_data/reader-fresh-root.pending" "$peer_data/reader-fresh-root" || return $?
    budget=$((deployment_start_deadline - SECONDS)); (( budget > 0 )) || return 2
    if (( budget > 15 )); then budget=15; fi
    compose_witness_wait "$peer_data/reader-crash-receipt.json" "$budget" || return $?
    printf 'Reader supervisor crash retired its stopped child; Engine restarted the same container with fresh scratch and a new session.\n'
}

compose_witness_crash_execute() {
    local identifier=$1 operation=$2 program=$3 probe_seconds=$4
    local operation_root=$scratch/reader-crash
    mkdir -m 700 -- "$operation_root/effects" || return $?
    # This probe's own window plus the existing five-second Engine response margin.
    if deployment_command supervisor-crash "$((probe_seconds + 5))" "$operation_root/exec" true exec "$identifier" \
        /opt/rdkit/environment/bin/python -I -B -c "$program" "$operation" "$probe_seconds"; then :
    else
        [[ $inference_retirement_confirmed == true && $inference_cancellation_status == 0 ]] || return 2
        printf 'Supervisor crash exec returned client status %s; protected probe cause: %s. Child exit is checked independently.\n' \
            "$deployment_command_status" "$operation_root/exec/response.stderr" >&2
    fi
}

compose_witness_old_id_rejected() {
    local identifier=$1 grace=$2 kind budget
    local operation_root=$scratch/late-old-id
    local -a arguments=()
    mkdir -m 700 -- "$operation_root" "$operation_root/effects" || return $?
    for kind in start stop; do
        if [[ $kind == start ]]; then budget=30; arguments=(start "$identifier")
        else budget=$((grace + 5)); arguments=(stop --time "$grace" "$identifier"); fi
        if deployment_command "$kind-api" "$budget" "$operation_root/$kind-api" true "${arguments[@]}"; then
            printf 'Late %s unexpectedly accepted an old ID after its confirmed removal.\n' "$kind" >&2
            return 2
        fi
        [[ $deployment_command_status == 1 && $inference_retirement_confirmed == true && $inference_cancellation_status == 0 ]] || return 2
    done
}

compose_witness_finish() {
    local status=$? cleanup=0
    trap - EXIT
    if [[ ${qualification_cleanup_held-false} == true ]]; then
        printf 'Qualification retains its original receipts and containers because failed-control evidence could not be preserved: %s\n' "$scratch" >&2
        exit 2
    fi
    if [[ $inference_retirement_confirmed != true ]]; then
        printf 'Qualification retains its original receipts and containers because the local Engine client has not retired: %s\n' "$scratch" >&2
        exit 2
    fi
    if [[ $deployment_installed == true ]]; then
        if make --no-print-directory -C "$repository_root" provider/deployment/down DEPLOYMENT="$deployment"; then :
        else
            printf 'Compose qualification retains its local peer and deployment receipts because physical shutdown is not confirmed. Peer ID: %s\n' "$peer_id" >&2
            exit 2
        fi
    fi
    if [[ -n $peer_id && $inference_retirement_confirmed == true ]]; then
        if (( status != 0 )); then
            if ! inference_native 'failed Compose qualification peer diagnostics' 10 stdio \
                "${engine[@]}" logs --tail 80 "$peer_id"; then
                printf 'Peer diagnostics were not delivered; its container and original receipts remain retained: %s\n' "$peer_id" >&2
                exit 2
            fi
        fi
        worker_container_retire "$peer_id" "$scratch/peer" "${engine[@]}" || cleanup=$?
        pending_diagnostic+=$worker_retirement_diagnostic
    elif [[ $peer_creation_attempted == true ]]; then
        printf 'Peer creation has no admitted cleanup handle. Preserve original create receipts: %s\n' "$scratch/peer" >&2
        cleanup=2
    fi
    if [[ $inference_retirement_confirmed == true ]]; then docker_client_cleanup || cleanup=$?; fi
    if [[ -n $pending_diagnostic ]]; then printf '%s\n' "$pending_diagnostic" >&2; fi
    if (( status == 0 && cleanup != 0 )); then status=$cleanup; fi
    printf 'Compose qualification evidence: %s\n' "$scratch"
    exit "$status"
}

compose_witness_main() {
    set -euo pipefail
    umask 077
    [[ $# -le 1 && $(id -u) != 0 && $(id -g) != 0 ]] || return 2
    local scientific_case=${1-pasted}
    case $scientific_case in pasted|smiles|references|capacity|successive|recovery) ;; *) return 2 ;; esac
    repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
    source "$repository_root/tools/docker_client.sh"
    source "$repository_root/tools/inference_native.sh"
    source "$repository_root/tools/worker_retirement.sh"
    source "$repository_root/tools/git_metadata.sh"
    source "$repository_root/tools/provider_image.sh"
    docker_client_directory= pending_diagnostic= inference_diagnostic= worker_retirement_diagnostic=
    inference_retirement_confirmed=true inference_cancellation_status=0
    qualification_cleanup_held=false
    unset deployment_runtime_root deployment_runtime_prefix
    deployment_resuming=false deployment_runtime_fence=false deployment_recovery_observed=false
    peer_id= deployment_installed=false peer_creation_attempted=false print_command=false
    scratch=$(mktemp -d /tmp/nmrnet-compose-XXXXXXXX)
    deployment=qualification-${scratch##*-}
    public=$repository_root/config/deployments/$deployment
    private=$repository_root/secrets/deployments/$deployment
    mkdir -p -m 700 -- "$public" "$private"
    peer_data=$scratch/data
    mkdir -m 700 -- "$scratch/peer" "$peer_data"
    local -a crash_grants=()
    if [[ $scientific_case == recovery ]]; then
        mkdir -m 700 -- "$private/runtime" "$private/runtime/reader" || return $?
        # The controlled client needs one fixture socket in this role's leaf.
        # It receives no host requests, receipts, store or other runtime leaf.
        crash_grants=(--mount "type=bind,src=$private/runtime/reader,dst=/reader-witness")
    fi
    trap compose_witness_finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    docker_client_prepare run || { pending_diagnostic+=$docker_client_diagnostic; return 2; }
    require_provider_image
    local capture=$scratch/source revision
    revision=$(repository_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}') || return $?
    printf '%s\n' "$revision" > "$scratch/source-revision"
    source "$repository_root/tools/git_inputs.sh"
    copy_git_blobs 'Compose qualification fixtures' "$repository_root" "$revision" "$capture" "$scratch/fixture-inputs" \
        tests/integration/provider_runtime/compose_peer.py tests/integration/provider_runtime/runner_crash_probe.py \
        tests/integration/provider_runtime/runner_crash_admission.py tests/integration/provider_core packages/provider-core/tests \
        tools/compose_runner_events.sh tools/compose_reply_loss.sh \
        tests/fixtures/preparation/expected.json tests/fixtures/preparation/smiles.json tests/fixtures/scientific_inputs \
        artifacts/reference-cpu-float32.json artifacts/prediction-cpu-float32.json || return $?
    local create_status=0
    peer_creation_attempted=true
    worker_retirement_command create "$scratch/peer" "${engine[@]}" create \
        --name "nmrnet-compose-peer-${scratch##*-}" --cidfile "$scratch/peer/worker.cid" --pull never \
        --init --network host --read-only --restart no --user "$(id -u):$(id -g)" \
        --cap-drop ALL --security-opt no-new-privileges:true --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
        --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 --log-opt mode=non-blocking --log-opt max-buffer-size=1m \
        --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=128m,mode=0700,uid=$(id -u),gid=$(id -g)" \
        --mount "type=bind,src=$peer_data,dst=$peer_data" \
        "${crash_grants[@]}" \
        --mount "type=bind,src=$capture/packages/provider-core/tests,dst=/fixture/core-tests,readonly" \
        --mount "type=bind,src=$capture/tests/integration/provider_core,dst=/fixture/binding-tests,readonly" \
        --mount "type=bind,src=$capture/tests/integration/provider_runtime/compose_peer.py,dst=/fixture/compose_peer.py,readonly" \
        --mount "type=bind,src=$capture/tests/fixtures/preparation/expected.json,dst=/fixture/expected.json,readonly" \
        --mount "type=bind,src=$capture/tests/fixtures/preparation/smiles.json,dst=/fixture/smiles.json,readonly" \
        --mount "type=bind,src=$capture/tests/fixtures/scientific_inputs,dst=/fixture/scientific_inputs,readonly" \
        --mount "type=bind,src=$capture/artifacts/reference-cpu-float32.json,dst=/fixture/reference-cpu-float32.json,readonly" \
        --mount "type=bind,src=$capture/artifacts/prediction-cpu-float32.json,dst=/fixture/prediction-cpu-float32.json,readonly" \
        --entrypoint /opt/provider/environment/bin/python "$provider_image_id" -I -B /fixture/compose_peer.py \
        --root "$peer_data" --store-root "$scratch/attempts" --seconds 420 --case "$scientific_case" || create_status=$?
    pending_diagnostic+=$worker_retirement_diagnostic
    worker_container_created "$scratch/peer" || { pending_diagnostic+=$worker_retirement_diagnostic; return 2; }
    peer_id=$worker_created_id
    (( create_status == 0 )) || return "$create_status"
    worker_retirement_command start "$scratch/peer" "${engine[@]}" start "$peer_id" || return $?
    compose_witness_wait "$peer_data/ready.json" 15 || return $?
    cp -- "$peer_data/provider.toml" "$peer_data/deployment.toml" "$public/" || return $?
    for input in signing.private.json interpreter.key api-ca.pem model-ca.pem store-ca.pem; do
        cp -- "$peer_data/configuration/$input" "$private/$input" || return $?
        chmod 600 -- "$private/$input" || return $?
    done
    make --no-print-directory -C "$repository_root" provider/deployment/install DEPLOYMENT="$deployment" || return $?
    deployment_installed=true
    if [[ $scientific_case == recovery ]]; then
        compose_witness_recovery || return $?
        make --no-print-directory -C "$repository_root" provider/deployment/up DEPLOYMENT="$deployment" || return $?
        cmp -s "$private/operation/mutation/intent" "$scratch/resumed-original.intent" \
            && [[ ! -e $private/operation/mutation/complete ]] || return 2
        make --no-print-directory -C "$repository_root" provider/deployment/down DEPLOYMENT="$deployment" || return $?
        deployment_installed=false
        : > "$peer_data/done"
        compose_witness_wait "$peer_data/receipt.json" 15 || return $?
        worker_retirement_command wait "$scratch/peer" "${engine[@]}" wait "$peer_id" || return $?
        cmp -s "$scratch/peer/wait.stdout" <(printf '0\n') || return 2
        return 0
    fi
    make --no-print-directory -C "$repository_root" provider/deployment/up DEPLOYMENT="$deployment" || return $?
    compose_witness_wait "$peer_data/completed.json" 180 || return $?
    if [[ $scientific_case == successive ]]; then compose_witness_native_events before-restart || return $?; fi
    make --no-print-directory -C "$repository_root" provider/deployment/status DEPLOYMENT="$deployment" || return $?
    make --no-print-directory -C "$repository_root" provider/deployment/down DEPLOYMENT="$deployment" || return $?
    make --no-print-directory -C "$repository_root" provider/deployment/up DEPLOYMENT="$deployment" || return $?
    # Repeated up is observe-only, and a restart cannot rediscover completed work.
    make --no-print-directory -C "$repository_root" provider/deployment/up DEPLOYMENT="$deployment" || return $?
    make --no-print-directory -C "$repository_root" provider/deployment/down DEPLOYMENT="$deployment" || return $?
    deployment_installed=false
    if [[ $scientific_case == successive ]]; then compose_witness_native_events after-restart || return $?; fi
    : > "$peer_data/done"
    compose_witness_wait "$peer_data/receipt.json" 15 || return $?
    worker_retirement_command wait "$scratch/peer" "${engine[@]}" wait "$peer_id" || return $?
    cmp -s "$scratch/peer/wait.stdout" <(printf '0\n') || return 2
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then compose_witness_main "$@"; fi
