#!/usr/bin/env bash
# Foreground host owner for the installed controller, three isolated routes and
# provider_workers.sh native allocations. No host Python, Docker socket grant,
# implicit pull or Job-selected host effect. See notes/003 D3 for provenance.

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

provider_host_bounds() {
    # Byte capacity belongs to the contained reader; Bash only forwards it.
    # Request counts and elapsed host seconds enter this host's arithmetic.
    if [[ ! $1 =~ ^[1-9][0-9]*$ ]]; then
        provider_host_refuse 'Startup capture requires an explicit positive aggregate byte ceiling.'; return 2
    fi
    if [[ ! $2 =~ ^[1-9][0-9]{0,5}$ || ! $3 =~ ^[1-9][0-9]{0,5}$ ]]; then
        provider_host_refuse 'Native request and host drain bounds require explicit positive integers of at most six digits.'; return 2
    fi
}

provider_host_adopt_plan() {
    local path=$1 field extra descriptor
    local -a fields=()
    if [[ $provider_host_capture_status != 0 || -v provider_host_ids[capture] ]]; then
        provider_host_refuse 'Cannot use startup handoff before successful helper completion and exact removal.'
        return 2
    fi
    if [[ ! -f $path || -L $path ]] || [[ $(stat -c '%u:%a' -- "$path") != "$(id -u):600" ]] \
        || (( $(stat -c '%s' -- "$path") > 65536 )); then
        provider_host_refuse 'Startup handoff requires a private regular file within its 65536-byte ceiling.'
        return 2
    fi
    exec {descriptor}< "$path" || return $?
    for ((field=0; field<17; field++)); do
        if ! IFS= read -r -d '' extra <&"$descriptor"; then
            exec {descriptor}<&-
            provider_host_refuse 'Startup helper did not return all 17 complete fields.'
            return 2
        fi
        fields+=("$extra")
    done
    if IFS= read -r -d '' extra <&"$descriptor" || [[ -n $extra ]]; then
        exec {descriptor}<&-
        provider_host_refuse 'Startup helper returned trailing data after its 17 fields.'
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
    local role=$1 network=$2 module=$3 create_status=0 directory=$provider_host_root/components/$1
    shift 3
    if [[ $provider_host_stop_requested == true ]]; then
        provider_host_refuse 'Service startup was interrupted before the next component was dispatched.'
        return 2
    fi
    mkdir -m 700 -- "$directory" "$directory/diagnostics" || return $?
    provider_host_ids[$role]=
    if provider_host_command create "$directory" create --cidfile "$directory/worker.cid" \
        --init --pull never --network "$network" --read-only --restart no --log-driver none \
        --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
        --mount "type=bind,src=$directory/diagnostics,dst=/diagnostics" \
        --entrypoint /bin/bash "${provider_host_grants[@]}" "$controller_image_id" -eu -c \
        'umask 077; ulimit -f 65536; exec /opt/provider/environment/bin/python -I -B -m "$@" > /diagnostics/stdout 2> /diagnostics/stderr' \
        service-component "$module" "$@"; then :; else create_status=$?; fi
    if worker_container_created "$directory"; then provider_host_ids[$role]=$worker_created_id
    else provider_host_diagnostic+=$worker_retirement_diagnostic; return 2; fi
    if [[ $create_status != 0 ]]; then return "$create_status"; fi
    local identifier=${provider_host_ids[$role]}
    provider_host_command posture "$directory" inspect --type container --format \
        '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.Running}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.NanoCpus}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{.Config.User}}|{{.HostConfig.RestartPolicy.Name}}' "$identifier" || return $?
    if ! cmp -s -- "$directory/posture.stdout" <(printf '%s|%s|created|false|true|%s|false|536870912|536870912|64|2000000000|["ALL"]|["no-new-privileges:true"]|%s:%s|no\n' \
        "$identifier" "$controller_image_id" "$network" "$(id -u)" "$(id -g)"); then
        provider_host_refuse 'Service component identity, isolation or resources differ from its fixed launch profile.'
        return 2
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
    local role=$1 directory=$provider_host_root/components/$1 identifier status running code
    provider_host_command observation "$directory" inspect --type container --format \
        '{{.Id}}|{{.State.Status}}|{{.State.Running}}|{{.State.ExitCode}}' "${provider_host_ids[$role]}" || return $?
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

provider_host_begin_drain() {
    if [[ $provider_host_term_attempted == true ]]; then return 0; fi
    provider_host_term_attempted=true
    provider_workers_creation_deadline=$((SECONDS + provider_host_drain_seconds))
    provider_host_command signal "$provider_host_root/components/controller" kill --signal TERM "${provider_host_ids[controller]}"
}

provider_host_drain() {
    [[ -v provider_host_ids[controller] ]] || return 0
    [[ -n ${provider_host_ids[controller]} ]] || return 2
    if [[ $provider_host_controller_running == false ]]; then return 0; fi
    provider_host_begin_drain || return $?
    while (( SECONDS < provider_workers_creation_deadline )); do
        provider_workers_service || { provider_host_diagnostic+=$provider_workers_diagnostic; return 2; }
        provider_host_observe controller || return $?
        [[ $provider_host_controller_running == true ]] || return 0
        sleep 0.1 || :
    done
    provider_workers_admission=closed
    provider_host_refuse 'The controller did not finish shutdown before the host drain deadline. Force exact retirement, retain the protected evidence and inspect durable Attempt state before restart.'
}

provider_host_finish() {
    local status=$1 role token phase cleanup=0
    trap - EXIT
    if [[ $inference_retirement_confirmed == true ]]; then
        provider_host_drain || cleanup=2
    fi
    if [[ $provider_host_controller_running == false && $provider_host_controller_exit != 0 ]]; then
        cleanup=2
        provider_host_diagnostic+='The controller exited unsuccessfully during shutdown; inspect its protected operation and cleanup evidence. '
    fi
    # Retire controller before emergency native cleanup. Never tear down its
    # grants while it might still submit or use them, including after rm fails.
    if [[ $inference_retirement_confirmed == true ]]; then provider_host_remove controller || cleanup=2; fi
    if [[ ! -v provider_host_ids[controller] && $inference_retirement_confirmed == true ]]; then
        if [[ $provider_host_native_open == true ]]; then
            provider_workers_admission=closed
            for phase in source prediction; do
                for token in "${!provider_worker_roles[@]}"; do
                    [[ ${provider_worker_retired[$token]} != true && -n ${provider_worker_roles[$token]} ]] || continue
                    if { [[ $phase == prediction && ${provider_worker_roles[$token]} == prediction ]] \
                        || [[ $phase == source && ${provider_worker_roles[$token]} != prediction ]]; }; then
                        if [[ -e $provider_workers_root/owned/$token/retirement ]]; then cleanup=2
                        else provider_workers_retire "$token" || cleanup=2; fi
                        [[ $inference_retirement_confirmed == true ]] || break 2
                    fi
                done
            done
            provider_host_diagnostic+=$provider_workers_diagnostic
        fi
        for role in api interpreter store capture; do
            [[ $inference_retirement_confirmed == true ]] || break
            provider_host_remove "$role" || cleanup=2
        done
        if [[ $provider_host_native_open == true && ${#provider_host_ids[@]} == 0 && $inference_retirement_confirmed == true ]]; then
            if provider_workers_close; then provider_host_native_open=false
            else cleanup=2; provider_host_diagnostic+=$provider_workers_diagnostic; fi
        fi
    fi
    if [[ $status == 0 && $cleanup != 0 ]]; then status=$cleanup; fi
    provider_host_diagnostic+=${pending_diagnostic-}
    if [[ -n $provider_host_diagnostic ]]; then printf '%s\n' "$provider_host_diagnostic" >&2 || status=2; fi
    if [[ $inference_retirement_confirmed != true || $provider_host_native_open == true || ${#provider_host_ids[@]} != 0 ]]; then
        printf 'Service ownership remains unresolved. Keep this host process and private evidence at %q; inspect the original receipts before recovery.\n' "$provider_host_root" >&2 || :
        # Repeated signals cannot discard the original native client or release
        # installation/native locks. Recovery is an explicit operator action.
        while :; do sleep 1 || :; done
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

provider_host_routes() {
    local role mode host peer identifier network_identity observed address prefix index=0
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
    if [[ " ${provider_host_modes[*]} " == *' local '* ]]; then
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
    if [[ " ${provider_host_modes[*]} " == *' external '* || " ${provider_host_modes[*]} " == *' mapped '* ]]; then
        [[ $provider_host_external_interface =~ ^[a-zA-Z0-9_.:-]{1,15}$ ]] \
            || { provider_host_refuse 'External and mapped service routes require an explicit host interface.'; return 2; }
    elif [[ -n $provider_host_external_interface ]]; then
        provider_host_refuse 'A host interface was supplied without an external or mapped service route.'; return 2
    fi
    if [[ " ${provider_host_modes[*]} " == *' external '* ]]; then
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
    for role in api interpreter store; do
        mode=${provider_host_modes[$index]}
        mapfile -t authorities <<< "${provider_host_fields[$((index + 3))]}"
        if [[ $mode == local ]]; then
            for peer in "${authorities[@]}"; do
                host=${peer%:*}
                if [[ ! -v peers[$host] ]]; then provider_host_refuse 'An admitted local authority has no explicit peer assignment.'; return 2; fi
                if [[ -v provider_host_addresses[$host] ]]; then continue; fi
                identifier=${peers[$host]}
                provider_host_command "peer-$role-${#provider_host_addresses[@]}" "$provider_host_root" inspect --type container --format \
                    "{{.Id}}|{{with index .NetworkSettings.Networks \"$provider_host_local_network\"}}{{.NetworkID}}|{{.IPAddress}}/{{.IPPrefixLen}}{{end}}" "$identifier" || return $?
                IFS='|' read -r observed network_identity address < "$provider_host_root/peer-$role-${#provider_host_addresses[@]}.stdout" || return $?
                prefix=${address#*/} address=${address%/*}
                if [[ $observed != "$identifier" || $network_identity != "$provider_host_network_id" || ! $prefix =~ ^[0-9]{1,2}$ ]] \
                    || ! provider_host_ipv4 "$address" \
                    || ! cmp -s -- "$provider_host_root/peer-$role-${#provider_host_addresses[@]}.stdout" <(printf '%s|%s|%s/%s\n' "$observed" "$network_identity" "$address" "$prefix"); then
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
        index=$((index + 1))
    done
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
    local role=$1 index=$2 value name key
    local -a authorities
    declare -A included=()
    provider_host_proxy_args=(--mode "${provider_host_modes[$index]}" --socket /route/proxy.sock --ready-file /route/ready)
    provider_host_grants=(--mount "type=bind,src=$provider_host_root/routes/$role,dst=/route")
    mapfile -t authorities <<< "${provider_host_fields[$((index + 3))]}"
    for value in "${authorities[@]}"; do provider_host_proxy_args+=(--authority "$value"); done
    if [[ ${provider_host_modes[$index]} == local ]]; then
        provider_host_proxy_network=$provider_host_network_id
        provider_host_proxy_args+=(--interface "$provider_host_local_interface")
    else
        provider_host_proxy_network=host
        provider_host_proxy_args+=(--interface "$provider_host_external_interface")
    fi
    if [[ ${provider_host_modes[$index]} == external ]]; then
        provider_host_grants+=(--mount "type=bind,src=$provider_host_root/resolv.conf,dst=/etc/resolv.conf,readonly")
    else
        for value in "${authorities[@]}"; do
            name=${value%:*}
            [[ ! -v included[$name] ]] || continue
            key=$name
            if [[ ${provider_host_modes[$index]} == mapped ]]; then key=mapped:$name; fi
            provider_host_proxy_args+=(--address "$name=${provider_host_addresses[$key]}")
            included[$name]=true
        done
    fi
}

provider_host_main() {
    if (( $# != 0 )); then printf '%s\n' 'Use make provider/run with explicit service selectors; see README.' >&2; return 2; fi
    local selector name value path role candidate mode revision stage upstream repository_revision index deadline status
    local -a required=(CONFIG SOURCE_ROOT INSTALLATION_ROOT MAXIMUM_STARTUP_BYTES MAXIMUM_NATIVE_REQUESTS HOST_DRAIN_SECONDS API_ROUTE INTERPRETER_ROUTE STORE_ROUTE)
    for selector in "${required[@]}"; do
        name=NMRNET_${selector}_INPUT
        [[ -n ${!name-} ]] || { printf 'Service startup requires explicit %s.\n' "$selector" >&2; return 2; }
    done
    if [[ $(id -u) == 0 || $(id -g) == 0 ]]; then printf '%s\n' 'Run the service as the nonroot owner of its private inputs and state.' >&2; return 2; fi
    repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
    provider_host_root=$NMRNET_INSTALLATION_ROOT_INPUT
    provider_host_diagnostic=
    for path in "$repository_root" "$NMRNET_SOURCE_ROOT_INPUT" "$NMRNET_CONFIG_INPUT" "$provider_host_root"; do provider_host_path "$path" || return $?; done
    provider_host_target "$NMRNET_SOURCE_ROOT_INPUT" || return $?
    provider_host_disjoint "$provider_host_root" "$NMRNET_SOURCE_ROOT_INPUT" || return $?
    provider_host_disjoint "$provider_host_root" "$repository_root" || return $?
    [[ -d $NMRNET_SOURCE_ROOT_INPUT && -f $NMRNET_CONFIG_INPUT && $NMRNET_CONFIG_INPUT == "$NMRNET_SOURCE_ROOT_INPUT/"* ]] \
        || { provider_host_refuse 'Config must be a regular file inside its explicit startup source root.'; return 2; }
    provider_host_bounds "$NMRNET_MAXIMUM_STARTUP_BYTES_INPUT" "$NMRNET_MAXIMUM_NATIVE_REQUESTS_INPUT" "$NMRNET_HOST_DRAIN_SECONDS_INPUT" || return $?
    provider_host_drain_seconds=$NMRNET_HOST_DRAIN_SECONDS_INPUT
    provider_host_modes=("$NMRNET_API_ROUTE_INPUT" "$NMRNET_INTERPRETER_ROUTE_INPUT" "$NMRNET_STORE_ROUTE_INPUT")
    for mode in "${provider_host_modes[@]}"; do [[ $mode == local || $mode == mapped || $mode == external ]] || { provider_host_refuse 'Each service route must explicitly select local, mapped or external.'; return 2; }; done
    provider_host_local_network=${NMRNET_LOCAL_NETWORK_INPUT-} provider_host_local_interface=${NMRNET_LOCAL_INTERFACE_INPUT-}
    provider_host_local_peers=${NMRNET_LOCAL_PEERS_INPUT-} provider_host_external_interface=${NMRNET_EXTERNAL_INTERFACE_INPUT-}
    provider_host_resolvers=${NMRNET_RESOLVERS_INPUT-}
    provider_host_mapped_addresses=${NMRNET_MAPPED_ADDRESSES_INPUT-}
    umask 077
    if [[ ! -e $provider_host_root ]]; then mkdir -m 700 -- "$provider_host_root" || return $?; fi
    [[ $(stat -c '%u:%a' -- "$provider_host_root") == "$(id -u):700" ]] \
        || { provider_host_refuse 'Installation root must be caller-owned with mode 0700.'; return 2; }
    exec {provider_host_lock}< "$provider_host_root" || return $?
    flock --nonblock "$provider_host_lock" || { provider_host_refuse 'Another host process owns this installation root.'; return 2; }
    if ! (shopt -s dotglob nullglob; entries=("$provider_host_root"/*); (( ${#entries[@]} == 0 ))); then
        provider_host_refuse 'Installation evidence already exists; inspect and archive the stopped installation before restarting.'; return 2
    fi
    source "$repository_root/tools/docker_client.sh"
    source "$repository_root/tools/inference_native.sh"
    source "$repository_root/tools/worker_retirement.sh"
    source "$repository_root/tools/provider_workers.sh"
    source "$repository_root/tools/git_metadata.sh"
    docker_client_directory= inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic= worker_retirement_diagnostic= pending_diagnostic=
    provider_host_stop_requested=false provider_host_term_attempted=false provider_host_native_open=false
    provider_host_controller_running=false provider_host_controller_exit=0 provider_host_capture_status=not-run
    declare -gA provider_host_ids=()
    trap 'provider_host_stop_requested=true' HUP INT TERM
    trap 'provider_host_finish "$?"' EXIT
    mkdir -m 700 -- "$provider_host_root/components" "$provider_host_root/output" "$provider_host_root/routes" || return $?
    docker_client_prepare run || { provider_host_diagnostic+=$docker_client_diagnostic; return 2; }
    repository_revision=$(repository_git -C "$repository_root" rev-parse --verify HEAD) || return $?
    read -r mode upstream stage path < <(repository_git -C "$repository_root" ls-files --stage -- NMRNetplusplus)
    [[ $repository_revision =~ ^[0-9a-f]{40}$ && $mode == 160000 && $upstream =~ ^[0-9a-f]{40}$ && $stage == 0 && $path == NMRNetplusplus ]] \
        || { provider_host_refuse 'Service startup requires full repository and unconflicted upstream revisions.'; return 2; }
    for role in inference preparation provider; do
        path=$repository_root/.work/$role/image-id
        [[ -f $path && ! -L $path ]] && IFS= read -r candidate < "$path" \
            && [[ $candidate =~ ^sha256:[0-9a-f]{64}$ ]] && cmp -s -- "$path" <(printf '%s\n' "$candidate") \
            || { provider_host_refuse 'Service images require complete immutable local image-ID records.'; return 2; }
        provider_host_command "$role-image" "$provider_host_root" image inspect --format '{{.Id}}' "$candidate" || return $?
        cmp -s -- "$provider_host_root/$role-image.stdout" <(printf '%s\n' "$candidate") \
            || { provider_host_refuse 'Recorded and observed service image identities differ.'; return 2; }
        case "$role" in inference) scientific_image_id=$candidate ;; preparation) geometry_image_id=$candidate ;; provider) controller_image_id=$candidate ;; esac
    done
    # These existing owners join immutable IDs to committed build inputs; an ID
    # alone cannot establish which binding/helper or RDKit runtime is installed.
    print_command=false
    source "$repository_root/tools/provider_image.sh"
    require_provider_image
    controller_image_id=$provider_image_id
    source "$repository_root/tools/geometry_image.sh"
    require_geometry_image
    provider_host_command upstream-image "$provider_host_root" image inspect --format '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}' "$scientific_image_id" || return $?
    cmp -s -- "$provider_host_root/upstream-image.stdout" <(printf '%s\n' "$upstream") \
        || { provider_host_refuse 'Prediction image upstream revision differs from the selected submodule revision.'; return 2; }
    provider_workers_capture_sources "$provider_host_root/native-inputs" "$repository_revision" || { provider_host_diagnostic+=$provider_workers_diagnostic; return 2; }
    provider_worker_prepared_root=$repository_root/.work/prepared
    for path in "$provider_worker_prepared_root/weight" "$provider_worker_prepared_root/checkpoints"; do
        provider_host_path "$path" && [[ -d $path ]] || { provider_host_refuse 'Prepared model roots must be existing canonical directories.'; return 2; }
        provider_host_disjoint "$path" "$NMRNET_SOURCE_ROOT_INPUT" || return $?
    done
    provider_host_grants=(--mount "type=bind,src=$NMRNET_SOURCE_ROOT_INPUT,dst=$NMRNET_SOURCE_ROOT_INPUT,readonly"
        --mount "type=bind,src=$provider_host_root/native-inputs,dst=/native-inputs,readonly"
        --mount "type=bind,src=$provider_host_root/output,dst=/output")
    provider_host_create capture none nmrnet_provider.launch_capture \
        --config "$NMRNET_CONFIG_INPUT" --source-root "$NMRNET_SOURCE_ROOT_INPUT" --destination /output/snapshot \
        --maximum-bytes "$NMRNET_MAXIMUM_STARTUP_BYTES_INPUT" --native-capture /native-inputs \
        --image-id "$scientific_image_id" --geometry-image-id "$geometry_image_id" --controller-image-id "$controller_image_id" \
        --repository-revision "$repository_revision" --upstream-revision "$upstream" || return $?
    provider_host_command wait "$provider_host_root/components/capture" wait "${provider_host_ids[capture]}" || return $?
    if ! cmp -s -- "$provider_host_root/components/capture/wait.stdout" <(printf '0\n'); then
        provider_host_refuse 'Startup helper failed; inspect its protected diagnostics before retrying.'; return 2
    fi
    provider_host_remove capture || return $?
    provider_host_capture_status=0
    provider_host_adopt_plan "$provider_host_root/components/capture/diagnostics/stdout" || return $?
    local store_root=${provider_host_fields[6]}
    provider_host_target "$store_root" || return $?
    for path in "$provider_host_root" "$NMRNET_SOURCE_ROOT_INPUT" "$provider_worker_prepared_root" "$repository_root"; do provider_host_disjoint "$store_root" "$path" || return $?; done
    if [[ ! -e $store_root ]]; then mkdir -m 700 -- "$store_root" || return $?; fi
    [[ $(stat -c '%u:%a' -- "$store_root") == "$(id -u):700" ]] || { provider_host_refuse 'Attempt store must be caller-owned with mode 0700.'; return 2; }
    provider_worker_accept_seconds=${provider_host_fields[7]} provider_worker_io_seconds=${provider_host_fields[8]}
    provider_source_io_seconds=${provider_host_fields[9]} provider_generation_io_seconds=${provider_host_fields[10]}
    provider_worker_embedding_seconds=${provider_host_fields[11]} provider_worker_seed=${provider_host_fields[12]}
    provider_worker_max_iterations=${provider_host_fields[13]}
    provider_workers_open "$provider_host_root/native" "$NMRNET_MAXIMUM_NATIVE_REQUESTS_INPUT" "${provider_host_fields[14]}" \
        || { provider_host_diagnostic+=$provider_workers_diagnostic; return 2; }
    provider_host_native_open=true
    provider_host_routes || return $?
    index=0
    for role in api interpreter store; do
        mkdir -m 700 -- "$provider_host_root/routes/$role" || return $?
        provider_host_proxy_arguments "$role" "$index"
        provider_host_create "$role" "$provider_host_proxy_network" tools.provider_proxy "${provider_host_proxy_args[@]}" || return $?
        deadline=$((SECONDS + 10))
        until [[ -S $provider_host_root/routes/$role/proxy.sock && -f $provider_host_root/routes/$role/ready ]] \
            && cmp -s -- "$provider_host_root/routes/$role/ready" <(printf 'ready\n'); do
            if (( SECONDS >= deadline )); then provider_host_refuse 'Service proxy did not publish its socket/readiness within ten seconds.'; return 2; fi
            sleep 0.05 || :
        done
        index=$((index + 1))
    done
    provider_host_grants=(--mount "type=bind,src=$provider_host_root/output/snapshot/inputs,dst=$NMRNET_SOURCE_ROOT_INPUT,readonly"
        --mount "type=bind,src=$provider_host_root/output/snapshot/execution.json,dst=/input/execution.json,readonly"
        --mount "type=bind,src=$provider_worker_source_root/artifacts/prediction-models.json,dst=/input/prediction-models.json,readonly"
        --mount "type=bind,src=$store_root,dst=$store_root"
        --mount "type=bind,src=$provider_workers_root/requests,dst=/native/requests"
        --mount "type=bind,src=$provider_workers_root/receipts,dst=/native/receipts,readonly"
        --mount "type=bind,src=$provider_workers_root/ipc,dst=/native/ipc,readonly")
    for role in api interpreter store; do provider_host_grants+=(--mount "type=bind,src=$provider_host_root/routes/$role,dst=/routes/$role,readonly"); done
    provider_host_create controller none nmrnet_provider.process --config "$NMRNET_CONFIG_INPUT" \
        --execution /input/execution.json --models /input/prediction-models.json --diagnostics /diagnostics \
        --api-proxy-socket /routes/api/proxy.sock --interpreter-proxy-socket /routes/interpreter/proxy.sock --store-proxy-socket /routes/store/proxy.sock || return $?
    provider_host_controller_running=true
    local last_probe=$SECONDS
    while [[ $provider_host_controller_running == true && $provider_host_stop_requested == false ]]; do
        provider_workers_service || { provider_host_diagnostic+=$provider_workers_diagnostic; return 2; }
        if [[ $provider_workers_admission == capacity-exhausted ]]; then
            provider_host_diagnostic+='Native request capacity is exhausted; stop admission and drain the controller. Inspect and archive the stopped installation before starting a fresh bounded run. '
            provider_host_stop_requested=true
            break
        fi
        if (( SECONDS >= last_probe + 2 )); then
            provider_host_observe controller || return $?
            for role in api interpreter store; do provider_host_observe "$role" || return $?; done
            last_probe=$SECONDS
        fi
        sleep 0.05 || :
    done
    if [[ $provider_host_controller_running == false && $provider_host_controller_exit != 0 ]]; then
        provider_host_refuse 'The controller exited unsuccessfully; its protected diagnostics retain the operation and cleanup evidence.'; return 2
    fi
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    set -euo pipefail
    provider_host_diagnostic=
    # Before installation ownership exists, only safe preflight diagnostics are
    # available. The owned caller replaces this trap before any engine dispatch.
    trap 'status=$?; if [[ -n $provider_host_diagnostic ]]; then printf "%s\n" "$provider_host_diagnostic" >&2 || status=2; fi; exit "$status"' EXIT
    provider_host_main "$@"
fi
