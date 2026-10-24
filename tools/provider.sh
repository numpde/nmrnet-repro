#!/usr/bin/env bash
# Source-only finite component and stopped-store recovery helpers. Production
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

provider_host_target() {
    provider_host_path "$1" || return $?
    case "$1/" in
        /bin/*|/sbin/*|/lib/*|/lib64/*|/usr/*|/opt/*|/etc/*|/proc/*|/sys/*|/dev/*|/tmp/|/native/*|/routes/*|/diagnostics/*|/input/*|/output/*|/native-inputs/*)
            provider_host_refuse 'Startup and store mounts must not mask image software, trust, kernel paths or fixed service grants.'
            return 2 ;;
    esac
}

provider_host_disjoint() {
    if [[ $1 == "$2" || $1 == "$2/"* || $2 == "$1/"* ]]; then
        provider_host_refuse 'Independent service grants must not overlap in either direction.'
        return 2
    fi
}

provider_host_adopt_plan() {
    local path=$1 count=$2 role=${3-capture} field extra descriptor
    local -a fields=()
    if [[ $count != 7 || ( $role != capture && $role != metadata ) ]]; then
        provider_host_refuse 'Recovery handoff requires seven fields and its exact helper role.'
        return 2
    fi
    if [[ $provider_host_capture_status != 0 || -v provider_host_ids[$role] ]]; then
        provider_host_refuse 'Cannot use startup handoff before successful helper completion and exact removal.'
        return 2
    fi
    if [[ ! -f $path || -L $path ]] || [[ $(stat -c '%u:%a' -- "$path") != "$(id -u):600" ]] \
        || (( $(stat -c '%s' -- "$path") > 65536 )); then
        provider_host_refuse 'Startup handoff requires a private regular file within its 65536-byte ceiling.'
        return 2
    fi
    exec {descriptor}< "$path" || return $?
    for ((field=0; field<count; field++)); do
        if ! IFS= read -r -d '' extra <&"$descriptor"; then
            exec {descriptor}<&-
            provider_host_refuse 'Startup helper did not return its complete fixed fields.'
            return 2
        fi
        fields+=("$extra")
    done
    if IFS= read -r -d '' extra <&"$descriptor" || [[ -n $extra ]]; then
        exec {descriptor}<&-
        provider_host_refuse 'Startup helper returned trailing data after its fixed fields.'
        return 2
    fi
    exec {descriptor}<&-
    provider_host_fields=("${fields[@]}")
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

provider_host_observe() {
    local role=$1 phase=$2 directory=$provider_host_root/components/$1 identifier status running code receipt
    if worker_retirement_command observation "$directory" "${engine[@]}" inspect --type container --format \
        '{{.Id}}|{{.State.Status}}|{{.State.Running}}|{{.State.ExitCode}}' "${provider_host_ids[$role]}"; then :
    else
        # A signal can interrupt this read-only poll while requesting shutdown.
        # Only the main liveness phase may hand that request to the drain owner;
        # cancelled output proves no daemon state, even when it looks complete.
        if [[ $phase == liveness && $inference_retirement_confirmed == true && -z $inference_diagnostic \
            && ( $inference_cancellation_status == 129 || $inference_cancellation_status == 130 || $inference_cancellation_status == 143 ) ]]; then
            for receipt in stdout stderr status; do
                if [[ -e $directory/interrupted-observation.$receipt || -L $directory/interrupted-observation.$receipt ]]; then
                    provider_host_diagnostic+=$worker_retirement_diagnostic
                    provider_host_refuse 'Cannot preserve another interrupted liveness observation without replacing its original evidence.'
                    return 2
                fi
            done
            # native_client publishes a handled signal as both its exit status
            # and cancellation fact. Preserve those facts before the next call.
            if printf 'native_status=%s\ncancellation=%s\nlocal_observer_retired=%s\n' \
                "$inference_cancellation_status" "$inference_cancellation_status" "$inference_retirement_confirmed" \
                > "$directory/interrupted-observation.status"; then :
            else provider_host_diagnostic+=$worker_retirement_diagnostic; return 2; fi
            for receipt in stdout stderr; do
                if [[ -e $directory/observation.$receipt || -L $directory/observation.$receipt ]]; then
                    if mv -- "$directory/observation.$receipt" "$directory/interrupted-observation.$receipt"; then :
                    else provider_host_diagnostic+=$worker_retirement_diagnostic; return 2; fi
                fi
            done
            provider_host_stop_requested=true
            return 3
        fi
        provider_host_diagnostic+=$worker_retirement_diagnostic
        if [[ $inference_cancellation_status != 0 ]]; then provider_host_stop_requested=true; fi
        return 2
    fi
    IFS='|' read -r identifier status running code < "$directory/observation.stdout" || return $?
    if [[ $identifier != "${provider_host_ids[$role]}" || ! $code =~ ^(0|[1-9][0-9]{0,2})$ ]] \
        || ! cmp -s -- "$directory/observation.stdout" <(printf '%s|%s|%s|%s\n' "$identifier" "$status" "$running" "$code"); then
        provider_host_refuse 'Service observation did not return a complete exact container state.'
        return 2
    fi
    if [[ $status == running && $running == true ]]; then
        # Only completed valid liveness observations are disposable. Keep failed
        # and final receipts without accumulating one poll file per second.
        rm -- "$directory/observation.stdout" "$directory/observation.stderr" || return $?
        return 0
    fi
    if [[ $role == controller && $status == exited && $running == false ]]; then
        provider_host_controller_running=false provider_host_controller_exit=$code
        return 0
    fi
    provider_host_refuse 'A service component stopped before its required handoff or controller shutdown.'
    return 2
}

provider_host_monitor() {
    local last_probe=$SECONDS role observed
    local -a roles=()
    case ${provider_host_recovery_mode-} in
        inspect) roles=(controller) ;;
        run) roles=(controller api) ;;
        *) provider_host_refuse 'Finite recovery monitoring requires inspect or run.'; return 2 ;;
    esac
    while [[ $provider_host_controller_running == true && $provider_host_stop_requested == false ]]; do
        if (( SECONDS >= last_probe + 2 )); then
            for role in "${roles[@]}"; do
                if provider_host_observe "$role" liveness; then :
                else
                    observed=$?
                    if [[ $observed == 3 ]]; then break 2; fi
                    return "$observed"
                fi
            done
            last_probe=$SECONDS
        fi
        sleep 0.05 || :
    done
    if [[ $provider_host_controller_running == false && $provider_host_controller_exit != 0 ]]; then
        provider_host_refuse 'The controller exited unsuccessfully; its protected diagnostics retain the operation and cleanup evidence.'; return 2
    fi
}

provider_host_begin_drain() {
    if [[ $provider_host_term_attempted == true ]]; then return 0; fi
    provider_host_term_attempted=true
    provider_host_drain_deadline=$((SECONDS + provider_host_drain_seconds))
    provider_host_command signal "$provider_host_root/components/controller" kill --signal TERM "${provider_host_ids[controller]}"
}

provider_host_drain() {
    [[ -v provider_host_ids[controller] ]] || return 0
    [[ -n ${provider_host_ids[controller]} ]] || return 2
    if [[ $provider_host_controller_running == false ]]; then return 0; fi
    provider_host_begin_drain || return $?
    while (( SECONDS < provider_host_drain_deadline )); do
        provider_host_observe controller drain || return $?
        [[ $provider_host_controller_running == true ]] || return 0
        sleep 0.1 || :
    done
    provider_host_refuse 'The controller did not finish shutdown before the host drain deadline. Force exact retirement, retain the protected evidence and inspect durable Attempt state before restart.'
}

provider_host_finish() {
    local status=$1 role cleanup=0
    trap - EXIT
    if [[ $inference_retirement_confirmed == true ]]; then
        provider_host_drain || cleanup=2
    fi
    if [[ $provider_host_controller_running == false && $provider_host_controller_exit != 0 ]]; then
        cleanup=2
        provider_host_diagnostic+='The controller exited unsuccessfully during shutdown; inspect its protected operation and cleanup evidence. '
    fi
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
    if [[ $status == 0 && ( ${provider_host_recovery_mode-} == inspect || ${provider_host_recovery_mode-} == run ) ]]; then
        printf 'Protected recovery result: %q\n' "$provider_host_root/components/controller/diagnostics/result.json" || status=2
        printf '%s\n' 'Next, read this result for remaining obligations before choosing further recovery or restarting the provider.' >&2 || status=2
    fi
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

provider_host_routes() {
    local mode host peer identifier network_identity observed address prefix
    local -a authorities mappings=()
    declare -A peers=() mapped=() used_mapped=()
    if [[ $provider_host_local_peers$provider_host_resolvers$provider_host_mapped_addresses == *$'\n'* || $provider_host_local_peers$provider_host_resolvers$provider_host_mapped_addresses == *$'\r'* ]]; then
        provider_host_refuse 'Peer and resolver lists must fit on one line; every supplied entry must be consumed.'; return 2
    fi
    read -r -a mappings <<< "$provider_host_local_peers"
    for peer in "${mappings[@]}"; do
        host=${peer%%=*} identifier=${peer#*=}
        if [[ ! $host =~ ^[a-z0-9][a-z0-9.-]*$ || ! $identifier =~ ^[0-9a-f]{64}$ || -v peers[$host] ]]; then
            provider_host_refuse 'Local peers require distinct canonical host=full-container-ID assignments.'; return 2
        fi
        peers[$host]=$identifier
    done
    read -r -a mappings <<< "$provider_host_mapped_addresses"
    for peer in "${mappings[@]}"; do
        host=${peer%%=*} address=${peer#*=}
        if [[ $peer != *=* || ! $host =~ ^[a-z0-9][a-z0-9.-]*$ || -v mapped[$host] ]] || ! provider_host_ipv4 "$address"; then
            provider_host_refuse 'Mapped routes require distinct canonical host=IPv4 assignments.'; return 2
        fi
        mapped[$host]=$address
    done
    if [[ $provider_host_api_mode == mapped ]]; then
        [[ $provider_host_mapped_interface =~ ^[a-zA-Z0-9_.:-]{1,15}$ ]] \
            || { provider_host_refuse 'Mapped service routes require an explicit MAPPED_INTERFACE.'; return 2; }
    elif [[ -n $provider_host_mapped_interface ]]; then
        provider_host_refuse 'A mapped interface was supplied without a mapped service route.'; return 2
    fi
    if [[ $provider_host_api_mode == local ]]; then
        [[ $provider_host_local_network =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ && $provider_host_local_interface =~ ^[a-zA-Z0-9_.:-]{1,15}$ ]] \
            || { provider_host_refuse 'Local service routes require an explicit named network and interface.'; return 2; }
        provider_host_command local-network "$provider_host_root" network inspect --format '{{.Id}}|{{.Name}}|{{.Internal}}' "$provider_host_local_network" || return $?
        IFS='|' read -r network_identity observed mode < "$provider_host_root/local-network.stdout" || return $?
        if [[ ! $network_identity =~ ^[0-9a-f]{64}$ || $observed != "$provider_host_local_network" || $mode != true ]] \
            || ! cmp -s -- "$provider_host_root/local-network.stdout" <(printf '%s|%s|true\n' "$network_identity" "$observed"); then
            provider_host_refuse 'Local service routes require an observed exact internal network.'; return 2
        fi
        provider_host_network_id=$network_identity
    elif [[ -n $provider_host_local_network$provider_host_local_interface$provider_host_local_peers ]]; then
        provider_host_refuse 'Local network grants were supplied without a local service route.'; return 2
    fi
    if [[ $provider_host_api_mode == external ]]; then
        [[ $provider_host_external_interface =~ ^[a-zA-Z0-9_.:-]{1,15}$ ]] \
            || { provider_host_refuse 'External service routes require an explicit EXTERNAL_INTERFACE.'; return 2; }
    elif [[ -n $provider_host_external_interface ]]; then
        provider_host_refuse 'An external interface was supplied without an external service route.'; return 2
    fi
    if [[ $provider_host_api_mode == external ]]; then
        local -a resolvers
        read -r -a resolvers <<< "$provider_host_resolvers"
        (( ${#resolvers[@]} > 0 )) || { provider_host_refuse 'External service routing requires at least one explicit IPv4 resolver.'; return 2; }
        for address in "${resolvers[@]}"; do
            provider_host_ipv4 "$address" && [[ $address != 127.* && $address != 0.* ]] \
                || { provider_host_refuse 'External resolvers must be canonical non-loopback IPv4 addresses.'; return 2; }
        done
        printf 'nameserver %s\n' "${resolvers[@]}" > "$provider_host_root/resolv.conf" || return $?
    elif [[ -n $provider_host_resolvers ]]; then
        provider_host_refuse 'Resolvers were supplied without an external service route.'; return 2
    fi
    declare -gA provider_host_addresses=()
    mode=$provider_host_api_mode
    mapfile -t authorities <<< "${provider_host_fields[4]}"
    if [[ $mode == local ]]; then
        for peer in "${authorities[@]}"; do
            host=${peer%:*}
            if [[ ! -v peers[$host] ]]; then provider_host_refuse 'An admitted local authority has no explicit peer assignment.'; return 2; fi
            if [[ -v provider_host_addresses[$host] ]]; then continue; fi
            identifier=${peers[$host]}
            provider_host_command "peer-api-${#provider_host_addresses[@]}" "$provider_host_root" inspect --type container --format \
                "{{.Id}}|{{with index .NetworkSettings.Networks \"$provider_host_local_network\"}}{{.NetworkID}}|{{.IPAddress}}/{{.IPPrefixLen}}{{end}}" "$identifier" || return $?
            IFS='|' read -r observed network_identity address < "$provider_host_root/peer-api-${#provider_host_addresses[@]}.stdout" || return $?
            prefix=${address#*/} address=${address%/*}
            if [[ $observed != "$identifier" || $network_identity != "$provider_host_network_id" || ! $prefix =~ ^[0-9]{1,2}$ ]] \
                || ! provider_host_ipv4 "$address" \
                || ! cmp -s -- "$provider_host_root/peer-api-${#provider_host_addresses[@]}.stdout" <(printf '%s|%s|%s/%s\n' "$observed" "$network_identity" "$address" "$prefix"); then
                provider_host_refuse 'A local peer lacks exact observed membership and an IPv4 address in the selected internal network.'; return 2
            fi
            provider_host_addresses[$host]=$address
        done
    elif [[ $mode == mapped ]]; then
        for peer in "${authorities[@]}"; do
            host=${peer%:*}
            [[ -v mapped[$host] ]] || { provider_host_refuse 'An admitted mapped authority has no explicit address.'; return 2; }
            provider_host_addresses[mapped:$host]=${mapped[$host]}
            used_mapped[$host]=true
        done
    fi

    for host in "${!peers[@]}"; do
        [[ -v provider_host_addresses[$host] ]] || { provider_host_refuse 'An unused local peer grant was supplied.'; return 2; }
    done
    for host in "${!mapped[@]}"; do
        [[ -v used_mapped[$host] ]] || { provider_host_refuse 'An unused mapped address grant was supplied.'; return 2; }
    done
}

provider_host_proxy_arguments() {
    # Address selection changes the TCP destination only. Keep every admitted
    # authority/port for CONNECT, TLS and signing; one hostname may use two ports.
    local value name key
    local -a authorities
    declare -A included=()
    # Finite proxies use their existing private diagnostics leaf for expiring
    # status. Production Compose roles have their own per-start /run tmpfs.
    provider_host_proxy_args=(--mode "$provider_host_api_mode" --socket /route/proxy.sock --ready-file /route/ready
        --status-directory /diagnostics)
    provider_host_grants=(--mount "type=bind,src=$provider_host_root/routes/api,dst=/route")
    mapfile -t authorities <<< "${provider_host_fields[4]}"
    for value in "${authorities[@]}"; do provider_host_proxy_args+=(--authority "$value"); done
    if [[ $provider_host_api_mode == local ]]; then
        provider_host_proxy_network=$provider_host_network_id
        provider_host_proxy_args+=(--interface "$provider_host_local_interface")
    else
        provider_host_proxy_network=host
        if [[ $provider_host_api_mode == mapped ]]; then
            provider_host_proxy_args+=(--interface "$provider_host_mapped_interface")
        else
            provider_host_proxy_args+=(--interface "$provider_host_external_interface")
        fi
    fi
    if [[ $provider_host_api_mode == external ]]; then
        provider_host_grants+=(--mount "type=bind,src=$provider_host_root/resolv.conf,dst=/etc/resolv.conf,readonly")
    else
        for value in "${authorities[@]}"; do
            name=${value%:*}
            [[ ! -v included[$name] ]] || continue
            key=$name
            if [[ $provider_host_api_mode == mapped ]]; then key=mapped:$name; fi
            provider_host_proxy_args+=(--address "$name=${provider_host_addresses[$key]}")
            included[$name]=true
        done
    fi
}

provider_host_recovery_arguments() {
    local phase=$1 path
    if [[ ${provider_host_recovery_mode-} != inspect && ${provider_host_recovery_mode-} != run ]]; then
        provider_host_refuse 'Recovery requires its fixed inspect or run operation.'; return 2
    fi
    provider_host_grants=()
    provider_host_argv=(/opt/provider/environment/bin/python -I -B -m)
    case "$phase" in
        metadata|capture)
            provider_host_grants+=(--mount "type=bind,src=$NMRNET_CONFIG_INPUT,dst=$NMRNET_CONFIG_INPUT,readonly")
            provider_host_argv+=(nmrnet_provider.recovery_capture "$phase" --mode "$provider_host_recovery_mode"
                --config "$NMRNET_CONFIG_INPUT" --source-root "$NMRNET_SOURCE_ROOT_INPUT"
                --maximum-bytes "$NMRNET_MAXIMUM_STARTUP_BYTES_INPUT" --handoff /diagnostics/host-fields)
            if [[ $phase == capture ]]; then
                if [[ $provider_host_recovery_mode == run ]]; then
                    for path in "${provider_host_fields[@]:5:2}"; do
                        [[ -n $path ]] || continue
                        provider_host_grants+=(--mount "type=bind,src=$path,dst=$path,readonly")
                    done
                fi
                provider_host_grants+=(--mount "type=bind,src=$provider_host_root/output,dst=/output")
                provider_host_argv+=(--destination /output/snapshot --expected-config-sha256 "${provider_host_fields[2]}")
            fi
            ;;
        controller)
            provider_host_grants+=(--mount "type=bind,src=$provider_host_root/output/snapshot/inputs,dst=$NMRNET_SOURCE_ROOT_INPUT,readonly"
                --mount "type=bind,src=${provider_host_fields[3]},dst=${provider_host_fields[3]}")
            provider_host_argv+=(nmrnet_provider.recovery "$provider_host_recovery_mode" --config "$NMRNET_CONFIG_INPUT" --diagnostics /diagnostics)
            if [[ $provider_host_recovery_mode == run ]]; then
                provider_host_grants+=(--mount "type=bind,src=$provider_host_root/routes/api,dst=/routes/api,readonly")
                provider_host_argv+=(--record-sha256 "$NMRNET_RECORD_SHA256_INPUT" --api-proxy-socket /routes/api/proxy.sock)
            fi
            ;;
        *) provider_host_refuse 'Recovery accepts only its metadata, capture and controller components.'; return 2 ;;
    esac
}

provider_host_recovery_main() {
    local mode=$1 selector name path deadline controller_image_id
    local -a required=(CONFIG SOURCE_ROOT INSTALLATION_ROOT MAXIMUM_STARTUP_BYTES HOST_DRAIN_SECONDS)
    if [[ $mode == run ]]; then required+=(API_ROUTE RECORD_SHA256); fi
    provider_host_diagnostic=
    for selector in "${required[@]}"; do
        name=NMRNET_${selector}_INPUT
        [[ -n ${!name-} ]] || { printf 'Stopped recovery requires explicit %s.\n' "$selector" >&2; return 2; }
    done
    if [[ $(id -u) == 0 || $(id -g) == 0 ]]; then
        provider_host_refuse 'Run stopped recovery as the nonroot owner of its private inputs and existing state.'; return 2
    fi
    if [[ ! $NMRNET_MAXIMUM_STARTUP_BYTES_INPUT =~ ^[1-9][0-9]*$ || ! $NMRNET_HOST_DRAIN_SECONDS_INPUT =~ ^[1-9][0-9]{0,5}$ ]]; then
        provider_host_refuse 'Recovery requires an explicit positive startup byte ceiling and finite host drain seconds.'; return 2
    fi
    if [[ $mode == run ]]; then
        [[ $NMRNET_RECORD_SHA256_INPUT =~ ^[0-9a-f]{64}$ ]] \
            || { provider_host_refuse 'Recovery run requires the exact lowercase record SHA-256 from inspection.'; return 2; }
        [[ $NMRNET_API_ROUTE_INPUT == local || $NMRNET_API_ROUTE_INPUT == mapped || $NMRNET_API_ROUTE_INPUT == external ]] \
            || { provider_host_refuse 'API recovery requires an explicit local, mapped or external route.'; return 2; }
    elif [[ $mode != inspect ]]; then
        provider_host_refuse 'Stopped recovery accepts only inspect or run.'; return 2
    elif [[ -n ${NMRNET_API_ROUTE_INPUT-}${NMRNET_RECORD_SHA256_INPUT-} ]]; then
        provider_host_refuse 'Offline inspection does not accept an API route or selected recovery record.'; return 2
    fi
    repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
    provider_host_root=$NMRNET_INSTALLATION_ROOT_INPUT
    provider_host_recovery_mode=$mode
    provider_host_drain_seconds=$NMRNET_HOST_DRAIN_SECONDS_INPUT
    for path in "$repository_root" "$NMRNET_SOURCE_ROOT_INPUT" "$NMRNET_CONFIG_INPUT" "$provider_host_root"; do provider_host_path "$path" || return $?; done
    provider_host_target "$NMRNET_SOURCE_ROOT_INPUT" || return $?
    provider_host_disjoint "$provider_host_root" "$NMRNET_SOURCE_ROOT_INPUT" || return $?
    provider_host_disjoint "$provider_host_root" "$repository_root" || return $?
    [[ -d $NMRNET_SOURCE_ROOT_INPUT && -f $NMRNET_CONFIG_INPUT && $NMRNET_CONFIG_INPUT == "$NMRNET_SOURCE_ROOT_INPUT/"* ]] \
        || { provider_host_refuse 'Recovery TOML must be a regular file inside the original startup source root.'; return 2; }
    provider_host_initialize || return $?
    print_command=false
    source "$repository_root/tools/provider_image.sh" || return $?
    require_provider_image || return $?
    controller_image_id=$provider_image_id
    provider_host_recovery_arguments metadata || return $?
    provider_host_create metadata none "$controller_image_id" provider_host_grants provider_host_argv false || return $?
    provider_host_command wait "$provider_host_root/components/metadata" wait "${provider_host_ids[metadata]}" || return $?
    cmp -s -- "$provider_host_root/components/metadata/wait.stdout" <(printf '0\n') \
        || { provider_host_refuse 'Recovery metadata helper failed; retain its protected diagnostics.'; return 2; }
    provider_host_remove metadata || return $?
    provider_host_capture_status=0
    provider_host_adopt_plan "$provider_host_root/components/metadata/diagnostics/host-fields" 7 metadata || return $?
    if [[ ${provider_host_fields[0]} != "$NMRNET_SOURCE_ROOT_INPUT" || "$NMRNET_SOURCE_ROOT_INPUT/${provider_host_fields[1]}" != "$NMRNET_CONFIG_INPUT" || ! ${provider_host_fields[2]} =~ ^[0-9a-f]{64}$ ]]; then
        provider_host_refuse 'Recovery metadata does not identify the original admitted TOML.'; return 2
    fi
    provider_host_target "${provider_host_fields[3]}" || return $?
    for path in "$provider_host_root" "$NMRNET_SOURCE_ROOT_INPUT" "$repository_root"; do provider_host_disjoint "${provider_host_fields[3]}" "$path" || return $?; done
    [[ -d ${provider_host_fields[3]} && ! -L ${provider_host_fields[3]} && $(stat -c '%u:%a' -- "${provider_host_fields[3]}") == "$(id -u):700" ]] \
        || { provider_host_refuse 'Stopped recovery requires the existing private caller-owned Attempt directory.'; return 2; }
    provider_host_capture_status=not-run
    provider_host_recovery_arguments capture || return $?
    provider_host_create capture none "$controller_image_id" provider_host_grants provider_host_argv false || return $?
    provider_host_command wait "$provider_host_root/components/capture" wait "${provider_host_ids[capture]}" || return $?
    cmp -s -- "$provider_host_root/components/capture/wait.stdout" <(printf '0\n') \
        || { provider_host_refuse 'Recovery input capture failed; retain its private partial output.'; return 2; }
    provider_host_remove capture || return $?
    provider_host_capture_status=0
    cmp -s -- "$provider_host_root/components/metadata/diagnostics/host-fields" "$provider_host_root/components/capture/diagnostics/host-fields" \
        || { provider_host_refuse 'Recovery capture changed its exact admitted metadata handoff.'; return 2; }
    provider_host_adopt_plan "$provider_host_root/components/capture/diagnostics/host-fields" 7 || return $?
    if [[ $mode == run ]]; then
        provider_host_api_mode=$NMRNET_API_ROUTE_INPUT
        provider_host_local_network=${NMRNET_LOCAL_NETWORK_INPUT-} provider_host_local_interface=${NMRNET_LOCAL_INTERFACE_INPUT-}
        provider_host_local_peers=${NMRNET_LOCAL_PEERS_INPUT-} provider_host_external_interface=${NMRNET_EXTERNAL_INTERFACE_INPUT-}
        provider_host_mapped_interface=${NMRNET_MAPPED_INTERFACE_INPUT-} provider_host_resolvers=${NMRNET_RESOLVERS_INPUT-}
        provider_host_mapped_addresses=${NMRNET_MAPPED_ADDRESSES_INPUT-}
        provider_host_routes || return $?
        mkdir -m 700 -- "$provider_host_root/routes/api" || return $?
        provider_host_proxy_arguments
        provider_host_argv=(/opt/provider/environment/bin/python -I -B -m tools.provider_proxy "${provider_host_proxy_args[@]}")
        provider_host_create api "$provider_host_proxy_network" "$controller_image_id" provider_host_grants provider_host_argv false || return $?
        deadline=$((SECONDS + 10))
        until [[ -S $provider_host_root/routes/api/proxy.sock && -f $provider_host_root/routes/api/ready ]] \
            && cmp -s -- "$provider_host_root/routes/api/ready" <(printf 'ready\n'); do
            if (( SECONDS >= deadline )); then provider_host_refuse 'Recovery API proxy did not become ready within ten seconds.'; return 2; fi
            sleep 0.05 || :
        done
    fi
    provider_host_recovery_arguments controller || return $?
    provider_host_create controller none "$controller_image_id" provider_host_grants provider_host_argv false || return $?
    provider_host_controller_running=true
    provider_host_monitor
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
    provider_host_stop_requested=false provider_host_term_attempted=false
    provider_host_controller_running=false provider_host_controller_exit=0 provider_host_capture_status=not-run
    declare -gA provider_host_ids=()
    trap 'provider_host_stop_requested=true' HUP INT TERM
    trap 'provider_host_finish "$?"' EXIT
    mkdir -m 700 -- "$provider_host_root/components" "$provider_host_root/output" "$provider_host_root/routes" || return $?
    docker_client_prepare run || { provider_host_diagnostic+=$docker_client_diagnostic; return 2; }
}
