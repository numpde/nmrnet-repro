#!/usr/bin/env bash
# Explicit, opt-in persistent Job; fixed scientific case, operator-selected API.
# The named Compose deployment owns production/native work. This caller owns only
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

api_witness_stop_deployment() {
    [[ ${api_deployment_active-false} == true ]] || return 0
    NMRNET_DEPLOYMENT_INPUT=$api_deployment_name bash "$repository_root/tools/provider_deployment.sh" down || return $?
    api_deployment_active=false
}

api_witness_finish() {
    local status=$? role cleanup=0
    trap - EXIT
    if [[ $inference_retirement_confirmed == true ]]; then
        if [[ ${api_witness_mode-} == real-ambiguity ]]; then
            for role in probe admission selection; do
                [[ $inference_retirement_confirmed == true ]] || break
                provider_host_remove "$role" || cleanup=2
            done
            # The route remains granted until every original helper is gone.
            # A failed exact retirement is held, even if its local client exited.
            if [[ ! -v provider_host_ids[probe] && ! -v provider_host_ids[admission] && ! -v provider_host_ids[selection] \
                && $inference_retirement_confirmed == true ]]; then provider_host_remove proxy || cleanup=2; fi
        else
            for role in client stage selection; do provider_host_remove "$role" || cleanup=2; done
            if [[ ! -v provider_host_ids[client] && ! -v provider_host_ids[stage] && ! -v provider_host_ids[selection] ]] \
                && api_witness_stop_deployment; then provider_host_remove peer || cleanup=2
            else cleanup=2; fi
        fi
    fi
    if [[ -n $provider_host_diagnostic ]]; then printf '%s\n' "$provider_host_diagnostic" >&2 || cleanup=2; fi
    if [[ $inference_retirement_confirmed != true ]]; then
        printf 'Qualification still owns an unretired local client at %q; keep this host process and its original receipts.\n' "$provider_host_root" >&2 || :
        while :; do sleep 1 || :; done
    fi
    if [[ ${api_deployment_active-false} == true || ${#provider_host_ids[@]} != 0 ]]; then
        printf 'Qualification stopped with unresolved deployment/helper obligations. Containers and original receipts remain at %q; their dependencies were retained.\n' "$provider_host_root" >&2 || :
        if [[ ${api_deployment_active-false} == true ]]; then
            source "$repository_root/tools/operator_guidance.sh"
            next_make_command 'Reconcile the original deployment before retiring its model peer:' \
                provider/deployment/down "DEPLOYMENT=$api_deployment_name" >&2 || :
        fi
        cleanup=2
    fi
    docker_client_cleanup || cleanup=2
    if [[ $status == 0 && $cleanup != 0 ]]; then status=$cleanup; fi
    if [[ $status == 0 ]]; then
        if [[ ${api_witness_mode-} == real-ambiguity ]]; then
            printf 'Selected-model interpreter probe and retirement qualified; no API Jobs or science: %s\n' "$provider_host_root"
        else printf 'Actual API prediction and retirement qualified; private evidence: %s\n' "$provider_host_root"; fi
    fi
    exit "$status"
}

api_witness_ambiguity() {
    # Both fixed probes share selection, grants, clocks and exact retirement.
    local probe=${1:-ambiguity} case_fixture=interpreter_ambiguity.json
    case "$probe" in
        ambiguity) ;;
        formats) case_fixture=interpreter_formats.json ;;
        *) printf '%s\n' 'Choose the maintained ambiguity or formats interpreter target.' >&2; return 2 ;;
    esac
    local selector alias path resolver interpreter_key interpreter_ca authority host_seconds deadline
    local -a resolvers=() common_grants=() admission_argv=()
    for selector in INTERPRETER_CONFIG EXTERNAL_INTERFACE RESOLVERS OUTPUT; do
        alias=NMRNET_${selector}_INPUT
        [[ -n ${!alias-} ]] || { printf 'Missing explicit ambiguity qualification selector: %s.\n' "$selector" >&2; return 2; }
    done
    [[ $(id -u) != 0 && $(id -g) != 0 ]] || return 2
    repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
    source "$repository_root/tools/provider.sh"
    provider_host_diagnostic= provider_host_root=$NMRNET_OUTPUT_INPUT
    provider_host_path "$provider_host_root" && provider_host_path "$NMRNET_INTERPRETER_CONFIG_INPUT" \
        && provider_host_disjoint "$provider_host_root" "$repository_root" \
        && provider_host_disjoint "$provider_host_root" "$NMRNET_INTERPRETER_CONFIG_INPUT" || return $?
    [[ $NMRNET_EXTERNAL_INTERFACE_INPUT =~ ^[a-zA-Z0-9_.:-]{1,15}$ \
        && $NMRNET_RESOLVERS_INPUT != *$'\n'* && $NMRNET_RESOLVERS_INPUT != *$'\r'* ]] \
        || { provider_host_refuse 'Ambiguity qualification requires explicit EXTERNAL_INTERFACE and single-line RESOLVERS.'; return 2; }
    read -r -a resolvers <<< "$NMRNET_RESOLVERS_INPUT"
    (( ${#resolvers[@]} > 0 )) || return 2
    for resolver in "${resolvers[@]}"; do
        provider_host_ipv4 "$resolver" && [[ $resolver != 0.* && $resolver != 127.* ]] \
            || { provider_host_refuse 'External resolvers require literal nonloopback IPv4 addresses.'; return 2; }
    done
    provider_host_initialize || return $?
    api_witness_mode=real-ambiguity foreground_pid= foreground_signalled=false
    trap api_witness_finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
    local print_command=false controller_image
    source "$repository_root/tools/provider_image.sh"
    require_provider_image || return $?
    controller_image=$provider_image_id
    mkdir -m 700 -- "$provider_host_root/selection" "$provider_host_root/admission" "$provider_host_root/probe" "$provider_host_root/routes/model" || return $?
    for resolver in "${resolvers[@]}"; do printf 'nameserver %s\n' "$resolver"; done > "$provider_host_root/resolv.conf"
    repository_git rev-parse HEAD > "$provider_host_root/source-revision.txt" || return $?
    sha256sum -- "$repository_root/Makefile" "$repository_root/tools/"{provider_api_qualification,provider,provider_logging,provider_image,docker_client,inference_native,worker_retirement,git_metadata}.sh \
        "$repository_root/tests/integration/provider_runtime/"{interpreter_ambiguity,stage_api_config}.py \
        "$repository_root/tests/integration/provider_core/config_ports.py" \
        "$repository_root/packages/provider-core/tests/__init__.py" \
        "$repository_root/packages/provider-core/tests/fakes/"{__init__,credential}.py \
        "$repository_root/tests/fixtures/preparation/$case_fixture" > "$provider_host_root/qualification-sources.sha256" || return $?
    printf '%s\n' "$controller_image" > "$provider_host_root/controller-image-id"
    common_grants=(--env NMRNET_IN_CONTAINER=1 --env PYTHONPATH=/fixture
        --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/interpreter_ambiguity.py,dst=/fixture/interpreter_ambiguity.py,readonly"
        --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/stage_api_config.py,dst=/fixture/stage_api_config.py,readonly"
        --mount "type=bind,src=$repository_root/tests/integration/provider_core/config_ports.py,dst=/fixture/config_ports.py,readonly"
        --mount "type=bind,src=$repository_root/packages/provider-core/tests/__init__.py,dst=/fixture/tests/__init__.py,readonly"
        --mount "type=bind,src=$repository_root/packages/provider-core/tests/fakes/__init__.py,dst=/fixture/tests/fakes/__init__.py,readonly"
        --mount "type=bind,src=$repository_root/packages/provider-core/tests/fakes/credential.py,dst=/fixture/tests/fakes/credential.py,readonly"
        --mount "type=bind,src=$NMRNET_INTERPRETER_CONFIG_INPUT,dst=/input/interpreter.toml,readonly")
    provider_host_grants=("${common_grants[@]}" --mount "type=bind,src=$provider_host_root/selection,dst=/output")
    provider_host_argv=(/opt/provider/environment/bin/python -B /fixture/stage_api_config.py --root /output --inspect-interpreter /input/interpreter.toml)
    provider_host_create selection none "$controller_image" provider_host_grants provider_host_argv true || return $?
    api_witness_wait selection 30 && provider_host_remove selection || return $?
    IFS= read -r interpreter_key < "$provider_host_root/selection/key_file.txt"
    IFS= read -r interpreter_ca < "$provider_host_root/selection/ca_file.txt"
    for path in "$interpreter_key" "$interpreter_ca"; do
        [[ -n $path ]] || continue
        provider_host_path "$path" && provider_host_disjoint "$path" "$provider_host_root" || return $?
    done
    common_grants+=(--mount "type=bind,src=$provider_host_root/selection/interpreter-inputs.json,dst=/input/interpreter-inputs.json,readonly"
        --mount "type=bind,src=$interpreter_key,dst=/input/interpreter.key,readonly")
    admission_argv=(--interpreter-config /input/interpreter.toml --selection /input/interpreter-inputs.json --key-file /input/interpreter.key --output /output --probe "$probe")
    if [[ -n $interpreter_ca ]]; then
        common_grants+=(--mount "type=bind,src=$interpreter_ca,dst=/input/interpreter-ca.pem,readonly")
        admission_argv+=(--ca-file /input/interpreter-ca.pem)
    fi
    provider_host_grants=("${common_grants[@]}" --mount "type=bind,src=$provider_host_root/admission,dst=/output")
    provider_host_argv=(/opt/provider/environment/bin/python -B /fixture/interpreter_ambiguity.py admit "${admission_argv[@]}")
    provider_host_create admission none "$controller_image" provider_host_grants provider_host_argv true || return $?
    api_witness_wait admission 30 && provider_host_remove admission || return $?
    IFS= read -r authority < "$provider_host_root/admission/authority.txt"
    IFS= read -r host_seconds < "$provider_host_root/admission/host-seconds.txt"
    [[ $host_seconds =~ ^[1-9][0-9]{0,5}$ ]] \
        && cmp -s -- "$provider_host_root/admission/host-seconds.txt" <(printf '%s\n' "$host_seconds") || return 2
    provider_host_grants=(--mount "type=bind,src=$provider_host_root/routes/model,dst=/route"
        --mount "type=bind,src=$provider_host_root/resolv.conf,dst=/etc/resolv.conf,readonly")
    provider_host_argv=(/opt/provider/environment/bin/python -I -B -m tools.provider_proxy --mode external --authority "$authority" \
        --interface "$NMRNET_EXTERNAL_INTERFACE_INPUT" --socket /route/proxy.sock --ready-file /route/ready)
    provider_host_create proxy host "$controller_image" provider_host_grants provider_host_argv true || return $?
    deadline=$((SECONDS + 10))
    until [[ -S $provider_host_root/routes/model/proxy.sock && -f $provider_host_root/routes/model/ready ]] \
        && cmp -s -- "$provider_host_root/routes/model/ready" <(printf 'ready\n'); do
        (( SECONDS < deadline )) || { provider_host_refuse 'The model proxy did not publish its sole route within ten seconds.'; return 2; }
        sleep 0.05 || :
    done
    provider_host_grants=("${common_grants[@]}"
        --mount "type=bind,src=$provider_host_root/admission/profile.json,dst=/input/profile.json,readonly"
        --mount "type=bind,src=$repository_root/tests/fixtures/preparation/$case_fixture,dst=/fixture/cases.json,readonly"
        --mount "type=bind,src=$provider_host_root/routes/model,dst=/route,readonly"
        --mount "type=bind,src=$provider_host_root/probe,dst=/output")
    provider_host_argv=(/opt/provider/environment/bin/python -B /fixture/interpreter_ambiguity.py run "${admission_argv[@]}" \
        --profile /input/profile.json --proxy-socket /route/proxy.sock --cases /fixture/cases.json)
    provider_host_create probe none "$controller_image" provider_host_grants provider_host_argv true || return $?
    api_witness_wait probe "$host_seconds" || return $?
    sha256sum --check --status "$provider_host_root/qualification-sources.sha256" \
        || { provider_host_refuse 'Qualification source bytes changed during the batch; retain its decisions without claiming source closure.'; return 2; }
}

api_witness_main() {
    if [[ $# == 1 && ( $1 == real-ambiguity || $1 == real-formats ) ]]; then api_witness_ambiguity "${1#real-}"; return $?; fi
    [[ $# == 1 && ( $1 == scripted-pasted || $1 == real-pasted || $1 == real-smiles || $1 == real-smiles-multiple || $1 == real-upload || $1 == real-sdf-zip ) ]] \
        || { printf '%s\n' 'Use a maintained make provider/qualify/api target with explicit selectors.' >&2; return 2; }
    local mode=$1 selector alias value path case_kind=pasted case_fixture=api_pasted.json
    if [[ $mode == real-smiles ]]; then case_kind=smiles case_fixture=api_smiles.json; fi
    if [[ $mode == real-smiles-multiple ]]; then case_kind=smiles-multiple case_fixture=api_smiles_multiple.json; fi
    if [[ $mode == real-upload ]]; then case_kind=upload case_fixture=api_upload.json; fi
    if [[ $mode == real-sdf-zip ]]; then case_kind=sdf-zip case_fixture=api_sdf_zip.json; fi
    local upload_receipt=${NMRNET_UPLOAD_RECEIPT_INPUT-}
    if [[ -n $upload_receipt && $mode != real-sdf-zip ]]; then
        printf '%s\n' 'UPLOAD_RECEIPT resumes only the SDF ZIP reservation after explicit storage reconciliation.' >&2; return 2
    fi
    local readback_seconds process_seconds host_seconds interpreter_route local_network= local_interface= local_address= external_interface= resolvers=
    local -a provider_host_argv=()
    local -a required=(API_ORIGIN TOPOLOGY PROJECT_REF PROVIDER_CREDENTIAL CA_FILE ADDRESS INTERFACE OUTPUT RETAINED_RESULT
        USER_ID CLIENT_CREDENTIAL_DIRECTORY API_CLIENT_IMAGE)
    if [[ $mode == scripted-pasted ]]; then
        required+=(LOCAL_NETWORK LOCAL_INTERFACE)
        readback_seconds=180 process_seconds=240 host_seconds=260 interpreter_route=local
    else
        required+=(INTERPRETER_CONFIG EXTERNAL_INTERFACE RESOLVERS)
        # Preparation240 + native prepare90/execute90/close90 leave room for
        # allocation, host retirement and publication. Held cleanup still holds.
        readback_seconds=900 process_seconds=960 host_seconds=980 interpreter_route=external
        if [[ $mode == real-upload ]]; then
            # Nine exchanges before readback have a 10-second socket timeout;
            # 120 seconds allows publication/setup. The whole alarm bounds trickle.
            process_seconds=1020 host_seconds=1040
        fi
        if [[ $mode == real-sdf-zip ]]; then process_seconds=1200 host_seconds=1220; fi
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
    local -a input_paths=("$NMRNET_PROVIDER_CREDENTIAL_INPUT" "$NMRNET_CA_FILE_INPUT" "$NMRNET_RETAINED_RESULT_INPUT"
        "$NMRNET_CLIENT_CREDENTIAL_DIRECTORY_INPUT")
    if [[ -n $upload_receipt ]]; then input_paths+=("$upload_receipt"); fi
    for path in "${input_paths[@]}"; do
        provider_host_path "$path" || return $?
        provider_host_disjoint "$path" "$provider_host_root" || return $?
    done
    if [[ $mode != scripted-pasted ]]; then
        provider_host_path "$NMRNET_INTERPRETER_CONFIG_INPUT" || return $?
        provider_host_disjoint "$NMRNET_INTERPRETER_CONFIG_INPUT" "$provider_host_root" || return $?
    fi
    local client_image=${NMRNET_API_CLIENT_IMAGE_INPUT-}
    provider_host_ipv4 "$NMRNET_ADDRESS_INPUT" \
        && [[ $NMRNET_INTERFACE_INPUT =~ ^[a-zA-Z0-9_.:-]{1,15}$ && $client_image =~ ^sha256:[0-9a-f]{64}$ ]] \
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
    mkdir -m 700 -- "$provider_host_root/components" "$provider_host_root/peer" "$provider_host_root/source" "$provider_host_root/deployment" "$provider_host_root/client"
    local qualification_source_root=$repository_root
    if [[ $mode == real-sdf-zip ]]; then
        source "$repository_root/tools/git_inputs.sh"
        source "$repository_root/tools/git_metadata.sh"
        local revision
        local -a inputs=(Makefile tools/provider_api_qualification.sh tools/provider.sh tools/provider_logging.sh
            tools/provider_deployment.sh tools/deployment_helper_grants.sh tools/deployment_runtime_recovery.sh tools/provider_deployment.py tools/operator_guidance.sh
            tools/provider_image.sh tools/docker_client.sh tools/inference_native.sh tools/worker_retirement.sh tools/git_inputs.sh tools/git_metadata.sh
            tools/bound_http_proxy.py tools/provider_proxy.py tools/container_guard.py
            tests/integration/provider_runtime/stage_api_config.py
            tests/integration/provider_runtime/api_prediction.py
            config/provider.toml.example
            tests/integration/provider_core/config_ports.py packages/provider-core/tests/__init__.py
            packages/provider-core/tests/fakes/__init__.py packages/provider-core/tests/fakes/credential.py
            "tests/fixtures/preparation/$case_fixture")
        if [[ $case_kind == sdf-zip ]]; then inputs+=(tests/fixtures/preparation/normative/butane_conformers.sdf); fi
        revision=$(repository_git -C "$repository_root" rev-parse HEAD) || return $?
        if [[ -n $(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${inputs[@]}") ]]; then
            printf '%s\n' 'Commit qualification inputs before capturing a persistent submission.' >&2; return 2
        fi
        qualification_source_root=$provider_host_root/qualification-source
        mkdir -m 700 -- "$qualification_source_root" || return $?
        copy_git_blobs 'Persistent submission qualification' "$repository_root" "$revision" "$qualification_source_root" "$provider_host_root/inputs" "${inputs[@]}" || return $?
        printf '%s\n' "$revision" > "$provider_host_root/qualification-revision"
    fi
    printf 'mode=%s\nreadback_seconds=%s\nprocess_seconds=%s\nhost_seconds=%s\n' \
        "$mode" "$readback_seconds" "$process_seconds" "$host_seconds" > "$provider_host_root/qualification-profile.txt"
    if [[ $mode == real-sdf-zip ]]; then
        (cd -- "$qualification_source_root"; sha256sum -- "${inputs[@]}") > "$provider_host_root/qualification-sources.sha256"
    else sha256sum -- "$repository_root/Makefile" "$repository_root/config/provider.toml.example" "$repository_root/tools/provider_api_qualification.sh" \
        "$repository_root/tools/provider.sh" "$repository_root/tools/provider_logging.sh" "$repository_root/tools/worker_retirement.sh" \
        "$repository_root/tools/inference_native.sh" "$repository_root/tools/docker_client.sh" \
        "$repository_root/tools/"{provider_deployment.sh,deployment_helper_grants.sh,deployment_runtime_recovery.sh,provider_deployment.py,operator_guidance.sh} \
        "$repository_root/tests/integration/provider_runtime/"{model_peer,stage_api_config,api_prediction}.py \
        "$repository_root/tests/integration/provider_core/config_ports.py" \
        "$repository_root/packages/provider-core/tests/fakes/"{tls_peer,conversation,credential}.py \
        "$repository_root/tests/fixtures/preparation/"{api_pasted,api_smiles,api_smiles_multiple,api_upload,smiles,expected}.json \
        "$repository_root/tests/fixtures/preparation/normative/atom_order.xyz" > "$provider_host_root/qualification-sources.sha256"; fi
    source "$repository_root/tools/docker_client.sh"
    source "$repository_root/tools/inference_native.sh"
    source "$repository_root/tools/worker_retirement.sh"
    docker_client_directory= inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic= worker_retirement_diagnostic= pending_diagnostic=
    declare -gA provider_host_ids=()
    api_witness_mode=$mode api_deployment_active=false provider_host_stop_requested=false
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
        provider_host_command membership "$provider_host_root" inspect --type container --format \
            "{{with index .NetworkSettings.Networks \"$network_name\"}}{{.IPAddress}}{{end}}" "${provider_host_ids[peer]}" || return $?
        IFS= read -r local_address < "$provider_host_root/membership.stdout" || return $?
        provider_host_ipv4 "$local_address" \
            && cmp -s "$provider_host_root/membership.stdout" <(printf '%s\n' "$local_address") || return 2
        deadline=$((SECONDS + 10))
        until [[ -f $provider_host_root/peer/ready.json ]]; do
            (( SECONDS < deadline )) || { provider_host_refuse 'The scripted model did not become ready within ten seconds.'; return 2; }
            sleep 0.1 || :
        done
    fi
    local -a stage_grants=(--env NMRNET_IN_CONTAINER=1 --env PYTHONPATH=/fixture
        --mount "type=bind,src=$qualification_source_root/config/provider.toml.example,dst=/fixture/provider.toml.example,readonly"
        --mount "type=bind,src=$qualification_source_root/tests/integration/provider_runtime/stage_api_config.py,dst=/fixture/stage.py,readonly"
        --mount "type=bind,src=$qualification_source_root/tests/integration/provider_core/config_ports.py,dst=/fixture/config_ports.py,readonly"
        --mount "type=bind,src=$qualification_source_root/packages/provider-core/tests/__init__.py,dst=/fixture/tests/__init__.py,readonly"
        --mount "type=bind,src=$qualification_source_root/packages/provider-core/tests/fakes/__init__.py,dst=/fixture/tests/fakes/__init__.py,readonly"
        --mount "type=bind,src=$qualification_source_root/packages/provider-core/tests/fakes/credential.py,dst=/fixture/tests/fakes/credential.py,readonly")
    if [[ $mode != scripted-pasted ]]; then
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
        --mount "type=bind,src=$provider_host_root/source,dst=$provider_host_root/source"
        --mount "type=bind,src=$provider_host_root/deployment,dst=$provider_host_root/deployment")
    provider_host_argv=(/opt/provider/environment/bin/python -B /fixture/stage.py \
        --root "$provider_host_root" --origin "$NMRNET_API_ORIGIN_INPUT" --topology "$NMRNET_TOPOLOGY_INPUT" \
        --deployment --api-interface "$NMRNET_INTERFACE_INPUT" --api-address "$NMRNET_ADDRESS_INPUT" \
        --interpreter-route "$interpreter_route")
    if [[ $mode == scripted-pasted ]]; then
        provider_host_argv+=(--interpreter-interface "$local_interface" --interpreter-network "$network_id" --interpreter-address "$local_address")
        provider_host_grants+=(
            --mount "type=bind,src=$provider_host_root/peer/ca.pem,dst=/input/model-ca.pem,readonly"
            --mount "type=bind,src=$provider_host_root/peer/ready.json,dst=/input/model-ready.json,readonly")
    else
        provider_host_argv+=(--interpreter-interface "$external_interface")
        local resolver
        local -a resolver_values=()
        read -r -a resolver_values <<< "$resolvers"
        for resolver in "${resolver_values[@]}"; do provider_host_argv+=(--resolver "$resolver"); done
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
    local public private input
    public=$(mktemp -d "$repository_root/config/deployments/qualification-api-XXXXXXXX") || return $?
    api_deployment_name=${public##*/}
    printf '%s\n' "$api_deployment_name" > "$provider_host_root/deployment-name" || return $?
    private=$repository_root/secrets/deployments/$api_deployment_name
    mkdir -p -m 700 -- "$repository_root/secrets/deployments" || return $?
    mkdir -m 700 -- "$private" || return $?
    cp -- "$provider_host_root/deployment/"{provider,deployment}.toml "$public/" || return $?
    for input in signing.private.json interpreter.key api-ca.crt model-ca.pem; do
        if [[ -f $provider_host_root/deployment/secrets/$input ]]; then
            cp -- "$provider_host_root/deployment/secrets/$input" "$private/$input" || return $?
            chmod 600 -- "$private/$input" || return $?
        fi
    done
    NMRNET_DEPLOYMENT_INPUT=$api_deployment_name bash "$repository_root/tools/provider_deployment.sh" install || return $?
    # Up can dispatch effects before returning failure. Its original receipts
    # must settle through down before the model peer's route can be withdrawn.
    api_deployment_active=true
    NMRNET_DEPLOYMENT_INPUT=$api_deployment_name bash "$repository_root/tools/provider_deployment.sh" up || return $?
    provider_host_grants=(--env NMRNET_IN_CONTAINER=1 --env PYTHONPATH=/fixture
        --mount "type=bind,src=$qualification_source_root/tests/integration/provider_runtime/api_prediction.py,dst=/fixture/api_prediction.py,readonly"
        --mount "type=bind,src=$qualification_source_root/tests/fixtures/preparation/$case_fixture,dst=/fixture/case.json,readonly"
        --mount "type=bind,src=$qualification_source_root/tools/bound_http_proxy.py,dst=/fixture/tools/bound_http_proxy.py,readonly"
        --mount "type=bind,src=$qualification_source_root/tools/container_guard.py,dst=/fixture/tools/container_guard.py,readonly"
        --mount "type=bind,src=$NMRNET_CLIENT_CREDENTIAL_DIRECTORY_INPUT/registration.json,dst=/credentials/registration.json,readonly"
        --mount "type=bind,src=$NMRNET_CLIENT_CREDENTIAL_DIRECTORY_INPUT/signing.private.json,dst=/credentials/signing.private.json,readonly"
        --mount "type=bind,src=$provider_host_root/source/api-ca.crt,dst=/input/api-ca.crt,readonly"
        --mount "type=bind,src=$provider_host_root/source/identity.json,dst=/input/identity.json,readonly"
        --mount "type=bind,src=$NMRNET_RETAINED_RESULT_INPUT,dst=/input/retained-result.json,readonly"
        --mount "type=bind,src=$provider_host_root/client,dst=/output")
    if [[ $mode == real-smiles || $mode == real-smiles-multiple ]]; then
        provider_host_grants+=(--mount "type=bind,src=$repository_root/tests/fixtures/preparation/smiles.json,dst=/fixture/smiles.json,readonly")
    fi
    if [[ $mode == real-upload ]]; then
        provider_host_grants+=(
            --mount "type=bind,src=$repository_root/tests/fixtures/preparation/expected.json,dst=/fixture/expected.json,readonly"
            --mount "type=bind,src=$repository_root/tests/fixtures/preparation/normative/atom_order.xyz,dst=/fixture/atom_order.xyz,readonly")
    fi
    if [[ $mode == real-sdf-zip ]]; then
        provider_host_grants+=(--mount "type=bind,src=$qualification_source_root/tests/fixtures/preparation/normative/butane_conformers.sdf,dst=/fixture/butane_conformers.sdf,readonly")
        if [[ -n $upload_receipt ]]; then
            provider_host_grants+=(--mount "type=bind,src=$upload_receipt,dst=/input/upload-receipt.json,readonly")
        fi
    fi
    provider_host_argv=(/usr/local/bin/python -B /fixture/api_prediction.py \
        --address "$NMRNET_ADDRESS_INPUT" --interface "$NMRNET_INTERFACE_INPUT" --project-ref "$NMRNET_PROJECT_REF_INPUT" --user-id "$NMRNET_USER_ID_INPUT" \
        --readback-seconds "$readback_seconds" --process-seconds "$process_seconds" --case-kind "$case_kind")
    if [[ -n $upload_receipt ]]; then provider_host_argv+=(--upload-receipt /input/upload-receipt.json); fi
    provider_host_create client host "$NMRNET_API_CLIENT_IMAGE_INPUT" provider_host_grants provider_host_argv true || return $?
    api_witness_wait client "$host_seconds" || return $?
    provider_host_remove client || return $?
    api_witness_stop_deployment || return $?
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
