#!/usr/bin/env bash
# Construct one offline pool-to-capture operation. Preview and execution share
# this create argv; execution owns its exact CID through shared retirement.

prepare_wheel_capture() {
    local context=$1 profile=$2 lock=$3 destination=$4 base=$5
    local capture_root=${destination%/*}
    local allocation=${6:-${capture_root%/*}/.wheel-capture.PRINT}
    wheel_capture=("${engine[@]}" create --cidfile "$allocation/worker.cid" --init --network none --pull never --read-only)
    wheel_capture+=(--user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true
        --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --env NMRNET_IN_CONTAINER=1 --workdir /workspace)
    local tool
    for tool in wheelhouse.py artifacts.py container_guard.py; do
        wheel_capture+=(--mount "type=bind,src=$context/tools/$tool,dst=/workspace/tools/$tool,readonly")
    done
    wheel_capture+=(--mount "type=bind,src=$context/requirements/$lock,dst=/input/requirements.lock,readonly"
        --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly"
        --mount "type=bind,src=$destination,dst=/capture")
    if [[ $profile == inference ]]; then
        wheel_capture+=(--mount "type=bind,src=$context/requirements/unicore-source.json,dst=/input/unicore-source.json,readonly")
    fi
    wheel_capture+=("$base" python3 -B -m tools.wheelhouse capture --profile "$profile"
        --lock /input/requirements.lock --pool /wheelhouse --output /capture)
    if [[ $profile == inference ]]; then wheel_capture+=(--source /input/unicore-source.json); fi
}

print_wheel_capture_plan() {
    local context=$1 profile=$2 lock=$3 destination=$4 base=$5
    local capture_root=${destination%/*}
    local allocation=${6:-${capture_root%/*}/.wheel-capture.PRINT}
    local identifier=CAPTURE_CONTAINER_ID_FROM_RECEIPT
    prepare_wheel_capture "$context" "$profile" "$lock" "$destination" "$base" "$allocation"
    printf '%q ' "${wheel_capture[@]}"; printf '\n'
    printf '%q ' "${engine[@]}" start --attach "$identifier"; printf '\n'
    printf '%q ' "${engine[@]}" stop --time 5 "$identifier"; printf '\n'
    printf '%q ' "${engine[@]}" inspect --type container --format \
        '{{.Id}} {{.State.Status}} {{.State.Running}} {{.State.Pid}} {{.State.Restarting}}' "$identifier"; printf '\n'
    printf '%q ' "${engine[@]}" logs --tail 80 "$identifier"; printf '>&2\n'
    printf '%q ' "${engine[@]}" rm "$identifier"; printf '\n'
}

execute_wheel_capture() {
    local context=$1 profile=$2 lock=$3 destination=$4 base=$5 budget=$6
    local parent create_status operation_status cleanup_status=0 operation_diagnostic retirement_diagnostic helper_directory
    if [[ ${wheel_capture_retirement_confirmed-} != true ]]; then
        printf -v inference_diagnostic '%s\n' 'Cannot start wheel capture: a previous wheel container retirement is unconfirmed.'
        return 2
    fi
    if [[ ! -v inference_retirement_confirmed || $inference_retirement_confirmed != true ]]; then
        printf -v inference_diagnostic '%s\n' 'Cannot start wheel capture: its original native client has not been confirmed stopped.'
        wheel_capture_retirement_confirmed=false
        return 2
    fi
    parent=${destination%/*}
    parent=${parent%/*}
    helper_directory=${BASH_SOURCE[0]%/*}
    # Source-only: use the same exact CID admission and retirement contract as
    # the scientific workers; no process or engine state is created by sourcing.
    if source "$helper_directory/worker_retirement.sh"; then :; else
        local source_status=$?
        printf -v inference_diagnostic 'Cannot load exact wheel-container retirement authority from %q (status %s); no engine operation started.\n' \
            "$helper_directory/worker_retirement.sh" "$source_status"
        return "$source_status"
    fi
    if wheel_capture_allocation_directory=$(umask 077; mktemp -d "$parent/.wheel-capture.XXXXXXXX" 2>/dev/null); then :; else
        local allocation_status=$?
        wheel_capture_allocation_directory=
        printf -v inference_diagnostic 'Cannot allocate private wheel-capture receipts under %q (status %s); no engine operation started.\n' \
            "$parent" "$allocation_status"
        return "$allocation_status"
    fi
    prepare_wheel_capture "$context" "$profile" "$lock" "$destination" "$base" "$wheel_capture_allocation_directory"

    # Receipts sit outside the build capture so failed engine diagnostics survive
    # its later removal. The caller's sticky fact protects any still-live grants.
    wheel_capture_retirement_confirmed=false
    worker_retirement_diagnostic=
    if worker_retirement_command create "$wheel_capture_allocation_directory" "${wheel_capture[@]}"; then
        create_status=0
    else
        create_status=$?
    fi
    operation_diagnostic=$worker_retirement_diagnostic
    if [[ $inference_retirement_confirmed != true || $inference_cancellation_status != 0 ]]; then
        printf -v inference_diagnostic '%sCannot admit wheel capture container: its create client was cancelled or not confirmed stopped. Preserve allocation and receipts at %q.' \
            "$operation_diagnostic" "$wheel_capture_allocation_directory"
        return "$create_status"
    fi
    if ! worker_container_created "$wheel_capture_allocation_directory"; then
        printf -v inference_diagnostic '%s%s Preserve allocation and receipts at %q.' \
            "$operation_diagnostic" "$worker_retirement_diagnostic" "$wheel_capture_allocation_directory"
        if [[ $create_status != 0 ]]; then return "$create_status"; fi
        return 2
    fi
    local identifier=$worker_created_id
    if [[ $create_status == 0 ]]; then
        if inference_native 'wheelhouse capture' "$budget" stdio "${engine[@]}" start --attach "$identifier"; then
            operation_status=0
        else
            operation_status=$?
        fi
        operation_diagnostic+=$inference_diagnostic
    else
        operation_status=$create_status
    fi
    if [[ $inference_retirement_confirmed != true || $inference_cancellation_status != 0 ]]; then
        printf -v inference_diagnostic '%sCannot confirm wheel capture retirement after cancellation or an unconfirmed native client. Preserve container %s and allocation %q.' \
            "$operation_diagnostic" "$identifier" "$wheel_capture_allocation_directory"
        return "$operation_status"
    fi

    if worker_container_retire "$identifier" "$wheel_capture_allocation_directory" "${engine[@]}"; then
        wheel_capture_retirement_confirmed=true
    else
        cleanup_status=$?
        retirement_diagnostic=$worker_retirement_diagnostic
    fi
    if [[ $wheel_capture_retirement_confirmed == true ]]; then
        inference_diagnostic=$operation_diagnostic
    else
        printf -v inference_diagnostic '%s%s Wheel capture container %s remains owned by allocation %q.' \
            "$operation_diagnostic" "$retirement_diagnostic" "$identifier" "$wheel_capture_allocation_directory"
    fi
    if [[ $operation_status != 0 || $cleanup_status != 0 ]]; then
        printf -v inference_diagnostic '%s Wheel-capture lifecycle receipts retained at %q.\n' \
            "$inference_diagnostic" "$wheel_capture_allocation_directory"
        if [[ $operation_status != 0 ]]; then return "$operation_status"; fi
        return "$cleanup_status"
    fi
    if rm -rf -- "$wheel_capture_allocation_directory"; then
        wheel_capture_allocation_directory=
    else
        local removal_status=$?
        printf -v inference_diagnostic '%sCannot remove retired wheel-capture receipts at %q (status %s).\n' \
            "$inference_diagnostic" "$wheel_capture_allocation_directory" "$removal_status"
        return "$removal_status"
    fi
}
