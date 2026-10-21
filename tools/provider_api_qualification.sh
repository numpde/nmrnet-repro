#!/usr/bin/env bash
# Explicit, opt-in persistent Job; fixed scientific case, operator-selected API.
# The normal provider.sh owns the foreground/native work. This caller owns only
# its optional scripted peer and finite configuration/public-client helpers, using
# the existing exact-CID/retirement owner. No key or host receipt reaches the peer.

api_witness_wait() {
    local role=$1 deadline=$((SECONDS + $2)) directory=$provider_host_root/components/$1 id state running code
    while (( SECONDS < deadline )); do
        provider_host_command observation "$directory" inspect --type container --format \
            '{{.Id}}|{{.State.Status}}|{{.State.Running}}|{{.State.ExitCode}}' "${provider_host_ids[$role]}" || return $?
        IFS='|' read -r id state running code < "$directory/observation.stdout" || return $?
        [[ $id == "${provider_host_ids[$role]}" && $code =~ ^(0|[1-9][0-9]{0,2})$ ]] \
            && cmp -s -- "$directory/observation.stdout" <(printf '%s|%s|%s|%s\n' "$id" "$state" "$running" "$code") \
            || { provider_host_refuse 'Qualification helper observation is incomplete or names a different container.'; return 2; }
        if [[ $state == exited && $running == false ]]; then
            [[ $code == 0 ]] || { provider_host_refuse 'Qualification helper failed; inspect its protected diagnostics and retained requests before any new mutation.'; return 2; }
            return 0
        fi
        [[ $state == running && $running == true ]] || { provider_host_refuse 'Qualification helper is neither running nor completely exited.'; return 2; }
        rm -- "$directory/observation.stdout" "$directory/observation.stderr" || return $?
        sleep 0.2 || :
    done
    provider_host_refuse 'Qualification helper exceeded its host deadline; retain requests and receipts, with no automatic retry.'
}

api_witness_stop_foreground() {
    [[ -n $foreground_pid ]] || return 0
    if [[ $foreground_signalled == false ]]; then
        foreground_signalled=true
        if kill -0 "$foreground_pid" 2>/dev/null; then kill -TERM "$foreground_pid" || return $?; fi
    fi
    # provider.sh owns cooperative settlement, including held native failures.
    # Wait for that original owner; a timeout cannot prove it released its work.
    while :; do
        if wait "$foreground_pid"; then foreground_pid=; return 0; fi
        if kill -0 "$foreground_pid" 2>/dev/null; then continue; fi
        foreground_pid=
        provider_host_refuse 'Foreground provider failed; inspect its protected diagnostics and durable Attempt state.'
        return 2
    done
}

api_witness_finish() {
    local status=$? role cleanup=0
    trap - EXIT
    if [[ $inference_retirement_confirmed == true ]]; then
        for role in client stage selection; do provider_host_remove "$role" || cleanup=2; done
        api_witness_stop_foreground || cleanup=2
        provider_host_remove peer || cleanup=2
    fi
    if [[ -n $provider_host_diagnostic ]]; then printf '%s\n' "$provider_host_diagnostic" >&2 || cleanup=2; fi
    if [[ -n $foreground_pid || ${#provider_host_ids[@]} != 0 || $inference_retirement_confirmed != true ]]; then
        printf 'Qualification ownership remains unresolved at %q; keep this host process and its original receipts.\n' "$provider_host_root" >&2 || :
        while :; do sleep 1 || :; done
    fi
    docker_client_cleanup || cleanup=2
    if [[ $status == 0 && $cleanup != 0 ]]; then status=$cleanup; fi
    if [[ $status == 0 ]]; then printf 'Actual API prediction and retirement qualified; private evidence: %s\n' "$provider_host_root"; fi
    exit "$status"
}

api_witness_main() {
    [[ $# == 1 && ( $1 == scripted-pasted || $1 == real-pasted ) ]] \
        || { printf '%s\n' 'Use make provider/qualify/api/pasted or provider/qualify/api/real-pasted with explicit selectors.' >&2; return 2; }
    local mode=$1 selector alias value path
    local readback_seconds process_seconds host_seconds interpreter_route local_network= local_interface= local_peers= external_interface= resolvers=
    local -a provider_host_argv=()
    local -a required=(API_ORIGIN TOPOLOGY PROJECT_REF USER_ID PROVIDER_CREDENTIAL CLIENT_CREDENTIAL_DIRECTORY CA_FILE ADDRESS INTERFACE OUTPUT API_CLIENT_IMAGE RETAINED_RESULT)
    if [[ $mode == scripted-pasted ]]; then
        required+=(LOCAL_NETWORK LOCAL_INTERFACE)
        readback_seconds=180 process_seconds=240 host_seconds=260 interpreter_route=local
    else
        required+=(INTERPRETER_CONFIG EXTERNAL_INTERFACE RESOLVERS)
        # Preparation240 + native prepare90/execute90/close90 leave room for
        # allocation, host retirement and publication. Held cleanup still holds.
        readback_seconds=900 process_seconds=960 host_seconds=980 interpreter_route=external
    fi
    for selector in "${required[@]}"; do
        alias=NMRNET_${selector}_INPUT
        value=${!alias-}
        [[ -n $value ]] || { printf 'Missing explicit command-line selector: %s. This opt-in lane creates a persistent Job.\n' "$selector" >&2; return 2; }
    done
    [[ $(id -u) != 0 && $(id -g) != 0 ]] || return 2
    repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
    source "$repository_root/tools/provider.sh"
    provider_host_diagnostic= provider_host_root=$NMRNET_OUTPUT_INPUT
    provider_host_path "$provider_host_root" || return $?
    provider_host_disjoint "$provider_host_root" "$repository_root" || return $?
    for path in "$NMRNET_PROVIDER_CREDENTIAL_INPUT" "$NMRNET_CLIENT_CREDENTIAL_DIRECTORY_INPUT" "$NMRNET_CA_FILE_INPUT" "$NMRNET_RETAINED_RESULT_INPUT"; do
        provider_host_path "$path" || return $?
        provider_host_disjoint "$path" "$provider_host_root" || return $?
    done
    if [[ $mode == real-pasted ]]; then
        provider_host_path "$NMRNET_INTERPRETER_CONFIG_INPUT" || return $?
        provider_host_disjoint "$NMRNET_INTERPRETER_CONFIG_INPUT" "$provider_host_root" || return $?
    fi
    provider_host_ipv4 "$NMRNET_ADDRESS_INPUT" \
        && [[ $NMRNET_INTERFACE_INPUT =~ ^[a-zA-Z0-9_.:-]{1,15}$ && $NMRNET_API_CLIENT_IMAGE_INPUT =~ ^sha256:[0-9a-f]{64}$ ]] \
        || { provider_host_refuse 'Qualification requires an explicit API interface, literal IPv4 publication and immutable client image ID.'; return 2; }
    if [[ $mode == scripted-pasted ]]; then
        [[ $NMRNET_LOCAL_INTERFACE_INPUT =~ ^[a-zA-Z0-9_.:-]{1,15}$ && $NMRNET_LOCAL_NETWORK_INPUT =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]] \
            || { provider_host_refuse 'Scripted interpretation requires an explicit named internal network and local interface.'; return 2; }
        local_network=$NMRNET_LOCAL_NETWORK_INPUT local_interface=$NMRNET_LOCAL_INTERFACE_INPUT
    else
        [[ $NMRNET_EXTERNAL_INTERFACE_INPUT =~ ^[a-zA-Z0-9_.:-]{1,15}$ ]] \
            || { provider_host_refuse 'Real interpretation requires an explicit EXTERNAL_INTERFACE.'; return 2; }
        external_interface=$NMRNET_EXTERNAL_INTERFACE_INPUT resolvers=$NMRNET_RESOLVERS_INPUT
    fi
    umask 077
    mkdir -m 700 -- "$provider_host_root" || return $?
    mkdir -m 700 -- "$provider_host_root/components" "$provider_host_root/peer" "$provider_host_root/source" "$provider_host_root/client"
    printf 'mode=%s\nreadback_seconds=%s\nprocess_seconds=%s\nhost_seconds=%s\n' \
        "$mode" "$readback_seconds" "$process_seconds" "$host_seconds" > "$provider_host_root/qualification-profile.txt"
    sha256sum -- "$repository_root/Makefile" "$repository_root/tools/provider_api_qualification.sh" \
        "$repository_root/tools/provider.sh" "$repository_root/tools/worker_retirement.sh" \
        "$repository_root/tools/inference_native.sh" "$repository_root/tools/docker_client.sh" \
        "$repository_root/tests/integration/provider_runtime/"{model_peer,stage_api_config,api_prediction}.py \
        "$repository_root/tests/integration/provider_core/config_ports.py" \
        "$repository_root/packages/provider-core/tests/fakes/"{tls_peer,conversation,credential}.py \
        "$repository_root/tests/fixtures/preparation/api_pasted.json" > "$provider_host_root/qualification-sources.sha256"
    source "$repository_root/tools/docker_client.sh"
    source "$repository_root/tools/inference_native.sh"
    source "$repository_root/tools/worker_retirement.sh"
    docker_client_directory= inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic= worker_retirement_diagnostic= pending_diagnostic=
    declare -gA provider_host_ids=()
    foreground_pid= foreground_signalled=false provider_host_stop_requested=false
    trap api_witness_finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
    docker_client_prepare run || return $?
    local controller_image network_id network_name internal host deadline interpreter_key interpreter_ca
    IFS= read -r controller_image < "$repository_root/.work/provider/image-id"
    [[ $controller_image =~ ^sha256:[0-9a-f]{64}$ ]] || return 2
    if [[ $mode == scripted-pasted ]]; then
        provider_host_command network "$provider_host_root" network inspect --format '{{.Id}}|{{.Name}}|{{.Internal}}' "$local_network" || return $?
        IFS='|' read -r network_id network_name internal < "$provider_host_root/network.stdout"
        [[ $network_id =~ ^[0-9a-f]{64}$ && $network_name == "$local_network" && $internal == true ]] \
            && cmp -s -- "$provider_host_root/network.stdout" <(printf '%s|%s|true\n' "$network_id" "$network_name") \
            || { provider_host_refuse 'The scripted model requires exact observed membership in a named internal network.'; return 2; }
        provider_host_grants=(--env NMRNET_IN_CONTAINER=1 --env PYTHONPATH=/fixture
            --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/model_peer.py,dst=/fixture/model_peer.py,readonly"
            --mount "type=bind,src=$repository_root/packages/provider-core/tests/fakes/tls_peer.py,dst=/fixture/tls_peer.py,readonly"
            --mount "type=bind,src=$repository_root/packages/provider-core/tests/fakes/conversation.py,dst=/fixture/conversation.py,readonly"
            --mount "type=bind,src=$repository_root/tests/fixtures/preparation/api_pasted.json,dst=/fixture/case.json,readonly"
            --mount "type=bind,src=$provider_host_root/peer,dst=/output")
        provider_host_argv=(/opt/provider/environment/bin/python -B /fixture/model_peer.py)
        provider_host_create peer "$network_id" "$controller_image" provider_host_grants provider_host_argv true || return $?
        local_peers="localhost=${provider_host_ids[peer]}"
        deadline=$((SECONDS + 10))
        until [[ -f $provider_host_root/peer/ready.json ]]; do
            (( SECONDS < deadline )) || { provider_host_refuse 'The scripted model did not become ready within ten seconds.'; return 2; }
            sleep 0.1 || :
        done
    fi
    local -a stage_grants=(--env NMRNET_IN_CONTAINER=1 --env PYTHONPATH=/fixture
        --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/stage_api_config.py,dst=/fixture/stage.py,readonly"
        --mount "type=bind,src=$repository_root/tests/integration/provider_core/config_ports.py,dst=/fixture/config_ports.py,readonly"
        --mount "type=bind,src=$repository_root/packages/provider-core/tests/__init__.py,dst=/fixture/tests/__init__.py,readonly"
        --mount "type=bind,src=$repository_root/packages/provider-core/tests/fakes/__init__.py,dst=/fixture/tests/fakes/__init__.py,readonly"
        --mount "type=bind,src=$repository_root/packages/provider-core/tests/fakes/credential.py,dst=/fixture/tests/fakes/credential.py,readonly")
    if [[ $mode == real-pasted ]]; then
        mkdir -m 700 -- "$provider_host_root/selection" || return $?
        provider_host_grants=("${stage_grants[@]}"
            --mount "type=bind,src=$NMRNET_INTERPRETER_CONFIG_INPUT,dst=/input/interpreter.toml,readonly"
            --mount "type=bind,src=$provider_host_root/selection,dst=/output")
        provider_host_argv=(/opt/provider/environment/bin/python -B /fixture/stage.py --root /output --inspect-interpreter /input/interpreter.toml)
        provider_host_create selection none "$controller_image" provider_host_grants provider_host_argv true || return $?
        api_witness_wait selection 30 || return $?
        provider_host_remove selection || return $?
        IFS= read -r interpreter_key < "$provider_host_root/selection/key_file.txt"
        IFS= read -r interpreter_ca < "$provider_host_root/selection/ca_file.txt"
        provider_host_path "$interpreter_key" && provider_host_disjoint "$interpreter_key" "$provider_host_root" || return $?
        if [[ -n $interpreter_ca ]]; then
            provider_host_path "$interpreter_ca" && provider_host_disjoint "$interpreter_ca" "$provider_host_root" || return $?
        fi
    fi
    provider_host_grants=("${stage_grants[@]}"
        --mount "type=bind,src=$NMRNET_PROVIDER_CREDENTIAL_INPUT,dst=/input/provider.json,readonly"
        --mount "type=bind,src=$NMRNET_CA_FILE_INPUT,dst=/input/api-ca.crt,readonly"
        --mount "type=bind,src=$provider_host_root/source,dst=$provider_host_root/source")
    provider_host_argv=(/opt/provider/environment/bin/python -B /fixture/stage.py \
        --root "$provider_host_root" --origin "$NMRNET_API_ORIGIN_INPUT" --topology "$NMRNET_TOPOLOGY_INPUT")
    if [[ $mode == scripted-pasted ]]; then
        provider_host_grants+=(
            --mount "type=bind,src=$provider_host_root/peer/ca.pem,dst=/input/model-ca.pem,readonly"
            --mount "type=bind,src=$provider_host_root/peer/ready.json,dst=/input/model-ready.json,readonly")
    else
        provider_host_grants+=(
            --mount "type=bind,src=$NMRNET_INTERPRETER_CONFIG_INPUT,dst=/input/interpreter.toml,readonly"
            --mount "type=bind,src=$provider_host_root/selection/interpreter-inputs.json,dst=/input/interpreter-inputs.json,readonly"
            --mount "type=bind,src=$interpreter_key,dst=/input/interpreter.key,readonly")
        if [[ -n $interpreter_ca ]]; then
            provider_host_grants+=(--mount "type=bind,src=$interpreter_ca,dst=/input/interpreter-ca.pem,readonly")
        fi
        provider_host_argv+=(--interpreter-config /input/interpreter.toml)
    fi
    provider_host_create stage none "$controller_image" provider_host_grants provider_host_argv true || return $?
    api_witness_wait stage 30 || return $?
    provider_host_remove stage || return $?
    IFS= read -r host < "$provider_host_root/source/host.txt"
    env NMRNET_CONFIG_INPUT="$provider_host_root/source/provider.toml" NMRNET_SOURCE_ROOT_INPUT="$provider_host_root/source" \
        NMRNET_INSTALLATION_ROOT_INPUT="$provider_host_root/installation" NMRNET_MAXIMUM_STARTUP_BYTES_INPUT=2097152 \
        NMRNET_MAXIMUM_NATIVE_REQUESTS_INPUT=8 NMRNET_HOST_DRAIN_SECONDS_INPUT=90 NMRNET_API_ROUTE_INPUT=mapped \
        NMRNET_INTERPRETER_ROUTE_INPUT="$interpreter_route" NMRNET_STORE_ROUTE_INPUT=mapped NMRNET_LOCAL_NETWORK_INPUT="$local_network" \
        NMRNET_LOCAL_INTERFACE_INPUT="$local_interface" NMRNET_LOCAL_PEERS_INPUT="$local_peers" \
        NMRNET_MAPPED_INTERFACE_INPUT="$NMRNET_INTERFACE_INPUT" NMRNET_EXTERNAL_INTERFACE_INPUT="$external_interface" NMRNET_MAPPED_ADDRESSES_INPUT="$host=$NMRNET_ADDRESS_INPUT" \
        NMRNET_RESOLVERS_INPUT="$resolvers" bash "$repository_root/tools/provider.sh" > "$provider_host_root/foreground.stdout" 2> "$provider_host_root/foreground.stderr" &
    foreground_pid=$!
    deadline=$((SECONDS + 45))
    until [[ -f $provider_host_root/installation/components/controller/diagnostics/admitted ]]; do
        kill -0 "$foreground_pid" 2>/dev/null && (( SECONDS < deadline )) \
            || { provider_host_refuse 'The foreground provider did not admit startup; inspect its retained diagnostics before creating a Job.'; return 2; }
        sleep 0.2 || :
    done
    provider_host_grants=(--env NMRNET_IN_CONTAINER=1 --env PYTHONPATH=/fixture
        --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/api_prediction.py,dst=/fixture/api_prediction.py,readonly"
        --mount "type=bind,src=$repository_root/tests/fixtures/preparation/api_pasted.json,dst=/fixture/case.json,readonly"
        --mount "type=bind,src=$repository_root/tools/bound_http_proxy.py,dst=/fixture/tools/bound_http_proxy.py,readonly"
        --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/fixture/tools/container_guard.py,readonly"
        --mount "type=bind,src=$NMRNET_CLIENT_CREDENTIAL_DIRECTORY_INPUT/registration.json,dst=/credentials/registration.json,readonly"
        --mount "type=bind,src=$NMRNET_CLIENT_CREDENTIAL_DIRECTORY_INPUT/signing.private.json,dst=/credentials/signing.private.json,readonly"
        --mount "type=bind,src=$provider_host_root/source/api-ca.crt,dst=/input/api-ca.crt,readonly"
        --mount "type=bind,src=$provider_host_root/source/identity.json,dst=/input/identity.json,readonly"
        --mount "type=bind,src=$NMRNET_RETAINED_RESULT_INPUT,dst=/input/retained-result.json,readonly"
        --mount "type=bind,src=$provider_host_root/client,dst=/output")
    provider_host_argv=(/usr/local/bin/python -B /fixture/api_prediction.py \
        --address "$NMRNET_ADDRESS_INPUT" --interface "$NMRNET_INTERFACE_INPUT" --project-ref "$NMRNET_PROJECT_REF_INPUT" --user-id "$NMRNET_USER_ID_INPUT" \
        --readback-seconds "$readback_seconds" --process-seconds "$process_seconds")
    provider_host_create client host "$NMRNET_API_CLIENT_IMAGE_INPUT" provider_host_grants provider_host_argv true || return $?
    api_witness_wait client "$host_seconds" || return $?
    provider_host_remove client || return $?
    api_witness_stop_foreground || return $?
    if [[ $mode == scripted-pasted ]]; then
        provider_host_command signal "$provider_host_root/components/peer" kill --signal TERM "${provider_host_ids[peer]}" || return $?
        api_witness_wait peer 10 || return $?
        # A stopped container alone does not prove the scripted peer's assertions
        # and TLS teardown passed. Its fixed receipt and successful exit do.
        cmp -s -- "$provider_host_root/peer/retired.json" <(printf '%s\n' '{"requests": 1, "handlers_settled": true}') \
            || { provider_host_refuse 'The model peer did not confirm its one exchange and settled handlers.'; return 2; }
        provider_host_remove peer || return $?
    fi
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    set -euo pipefail
    api_witness_main "$@"
fi
