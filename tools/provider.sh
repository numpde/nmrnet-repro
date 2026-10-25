#!/usr/bin/env bash
# Source-only finite component helpers. Production
# runs through provider_deployment.sh; no host process pumps native requests.
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    printf '%s\n' 'This file supplies finite helpers. Run make help for supported provider/deployment commands.' >&2
    exit 2
fi

provider_host_refuse() {
    provider_host_diagnostic+="$1 "
    return 2
}

provider_host_path() {
    local path=$1
    if [[ $path != /* || $path == / || $path == *','* || $path == *$'\n'* || $path == *$'\r'* ]] \
        || [[ $(realpath -m -- "$path") != "$path" ]]; then
        provider_host_refuse 'Service paths must be canonical absolute paths without links or mount delimiters.'
        return 2
    fi
}

provider_host_disjoint() {
    if [[ $1 == "$2" || $1 == "$2/"* || $2 == "$1/"* ]]; then
        provider_host_refuse 'Independent service grants must not overlap in either direction.'
        return 2
    fi
}

provider_host_command() {
    local operation=$1 directory=$2
    shift 2
    if worker_retirement_command "$operation" "$directory" "${engine[@]}" "$@"; then return 0; fi
    provider_host_diagnostic+=$worker_retirement_diagnostic
    if [[ $inference_cancellation_status != 0 ]]; then provider_host_stop_requested=true; fi
    return 2
}

provider_host_create() {
    local role=$1 network=$2 image=$3 record_mounts=$6 create_status=0 directory=$provider_host_root/components/$1
    local -n component_grants=$4 component_command=$5
    local memory=512m memory_bytes=536870912 pids=64 temporary=64m
    source "$(dirname -- "${BASH_SOURCE[0]}")/provider_logging.sh" || return $?
    provider_logging_configure || return $?
    if [[ $provider_host_stop_requested == true ]]; then
        provider_host_refuse 'Service startup was interrupted before the next component was dispatched.'
        return 2
    fi
    mkdir -m 700 -- "$directory" "$directory/diagnostics" || return $?
    provider_host_ids[$role]=
    if provider_host_command create "$directory" create --cidfile "$directory/worker.cid" \
        --init --pull never --network "$network" --read-only --restart no "${provider_logging_options[@]}" \
        --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus 2 --memory "$memory" --memory-swap "$memory" --pids-limit "$pids" \
        --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=$temporary" \
        --mount "type=bind,src=$directory/diagnostics,dst=/diagnostics" \
        --env "NMRNET_LOG_LEVEL=$provider_log_level" \
        --entrypoint /bin/bash "${component_grants[@]}" "$image" -eu -c \
        'umask 077; ulimit -f 65536; exec "$@"' \
        service-component "${component_command[@]}"; then :; else create_status=$?; fi
    if worker_container_created "$directory"; then provider_host_ids[$role]=$worker_created_id
    else provider_host_diagnostic+=$worker_retirement_diagnostic; return 2; fi
    if [[ $create_status != 0 ]]; then return "$create_status"; fi
    local identifier=${provider_host_ids[$role]}
    provider_host_command posture "$directory" inspect --type container --format \
        '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.Running}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.NanoCpus}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{.Config.User}}|{{.HostConfig.RestartPolicy.Name}}|{{.HostConfig.LogConfig.Type}}|{{json .HostConfig.LogConfig.Config}}' "$identifier" || return $?
    if ! cmp -s -- "$directory/posture.stdout" <(printf '%s|%s|created|false|true|%s|false|%s|%s|%s|2000000000|["ALL"]|["no-new-privileges:true"]|%s:%s|no|%s\n' \
        "$identifier" "$image" "$network" "$memory_bytes" "$memory_bytes" "$pids" "$(id -u)" "$(id -g)" "$provider_logging_posture"); then
        provider_host_refuse 'Service component identity, isolation or resources differ from its fixed launch profile.'
        return 2
    fi
    # Finite callers select mount receipts explicitly in addition to the fixed
    # resource/isolation admission; the controller receives no Engine authority.
    if [[ $record_mounts == true ]]; then
        provider_host_command mounts "$directory" inspect --type container --format '{{json .Mounts}}' "$identifier" || return $?
    fi
    provider_host_command start "$directory" start "$identifier"
}

provider_host_remove() {
    local role=$1 directory=$provider_host_root/components/$1
    [[ -v provider_host_ids[$role] ]] || return 0
    if [[ -z ${provider_host_ids[$role]} || -e $directory/retirement || -L $directory/retirement ]]; then
        provider_host_refuse 'A component has an uncertain create or prior retirement; retain its original ownership and receipts.'
        return 2
    fi
    if worker_container_retire "${provider_host_ids[$role]}" "$directory" "${engine[@]}"; then
        unset "provider_host_ids[$role]"
        return 0
    fi
    provider_host_diagnostic+=$worker_retirement_diagnostic
    return 2
}

provider_host_finish() {
    local status=$1 role cleanup=0
    trap - EXIT
    # Retire the finite controller before its API route. Never tear down its
    # grants while it might still submit or use them, including after rm fails.
    if [[ $inference_retirement_confirmed == true ]]; then provider_host_remove controller || cleanup=2; fi
    if [[ ! -v provider_host_ids[controller] && $inference_retirement_confirmed == true ]]; then
        for role in api capture metadata; do
            [[ $inference_retirement_confirmed == true ]] || break
            provider_host_remove "$role" || cleanup=2
        done
    fi
    if [[ $status == 0 && $cleanup != 0 ]]; then status=$cleanup; fi
    provider_host_diagnostic+=${pending_diagnostic-}
    if [[ -n $provider_host_diagnostic ]]; then printf '%s\n' "$provider_host_diagnostic" >&2 || status=2; fi
    if [[ $inference_retirement_confirmed != true ]]; then
        printf 'A local recovery client remains unretired. Keep this process and its private evidence at %q.\n' "$provider_host_root" >&2 || :
        while :; do sleep 1 || :; done
    fi
    if (( ${#provider_host_ids[@]} )); then
        printf 'Recovery left container ownership unresolved. Preserve original IDs, dependencies and receipts at %q before arranging another recovery.\n' "$provider_host_root" >&2 || :
        status=2
    fi
    docker_client_cleanup || status=2
    exec {provider_host_lock}<&-
    exit "$status"
}

provider_host_ipv4() {
    local value=$1 part
    local -a parts
    [[ $value =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || return 2
    IFS=. read -r -a parts <<< "$value"
    for part in "${parts[@]}"; do
        [[ $part == 0 || $part != 0* ]] && (( 10#$part <= 255 )) || return 2
    done
}

provider_host_initialize() {
    # Each finite caller owns a fresh private root and local Engine client.
    # Production native ownership belongs to the named Compose deployment.
    umask 077
    if [[ ! -e $provider_host_root ]]; then mkdir -m 700 -- "$provider_host_root" || return $?; fi
    [[ $(stat -c '%u:%a' -- "$provider_host_root") == "$(id -u):700" ]] \
        || { provider_host_refuse 'Installation root must be caller-owned with mode 0700.'; return 2; }
    exec {provider_host_lock}< "$provider_host_root" || return $?
    flock --nonblock "$provider_host_lock" || { provider_host_refuse 'Another host process owns this installation root.'; return 2; }
    if ! (shopt -s dotglob nullglob; entries=("$provider_host_root"/*); (( ${#entries[@]} == 0 ))); then
        provider_host_refuse 'Installation evidence already exists; preserve the stopped evidence and choose a fresh empty installation root.'; return 2
    fi
    source "$repository_root/tools/docker_client.sh" || return $?
    source "$repository_root/tools/inference_native.sh" || return $?
    source "$repository_root/tools/worker_retirement.sh" || return $?
    source "$repository_root/tools/git_metadata.sh" || return $?
    docker_client_directory= inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic= worker_retirement_diagnostic= pending_diagnostic=
    provider_host_stop_requested=false
    declare -gA provider_host_ids=()
    trap 'provider_host_stop_requested=true' HUP INT TERM
    trap 'provider_host_finish "$?"' EXIT
    mkdir -m 700 -- "$provider_host_root/components" "$provider_host_root/output" "$provider_host_root/routes" || return $?
    docker_client_prepare run || { provider_host_diagnostic+=$docker_client_diagnostic; return 2; }
}
