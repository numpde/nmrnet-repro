#!/usr/bin/env bash
# Exercise the installed host commands with synthetic retained work and loopback
# TLS only. The existing host/client owners retain every unresolved obligation.

recovery_witness_wait_file() {
    local path=$1 deadline=$((SECONDS + 15))
    while [[ ! -f $path || -L $path ]]; do
        if (( SECONDS >= deadline )); then
            provider_host_refuse 'The local recovery fixture did not publish its next protected handoff within fifteen seconds.'
            return 2
        fi
        sleep 0.05 || :
    done
}

recovery_witness_wait_foreground() {
    local deadline=$((SECONDS + 30)) status
    while kill -0 "$foreground_pid" 2>/dev/null; do
        if (( SECONDS >= deadline )); then
            provider_host_refuse 'The recovery host did not finish within thirty seconds; settle the original host before releasing its peer.'
            return 2
        fi
        sleep 0.05 || :
    done
    if wait "$foreground_pid"; then status=0; else status=$?; fi
    foreground_pid=
    if [[ $status != 0 ]]; then
        provider_host_refuse 'The recovery host failed; preserve its protected operation and exact-retirement receipts.'
        return 2
    fi
}

recovery_witness_finish() {
    local status=$?
    trap - EXIT
    if [[ $inference_retirement_confirmed == true ]]; then
        if api_witness_stop_foreground; then :; else status=2; fi
    fi
    if [[ -n $foreground_pid ]]; then
        printf 'The original foreground host remains owned at %q; keep this process and its local TLS peer.\n' "$provider_host_root" >&2 || :
        while :; do sleep 1 || :; done
    fi
    if [[ $inference_retirement_confirmed == true ]]; then api_witness_stop_deployment || status=2; fi
    if [[ ${api_deployment_active-false} == true ]]; then
        printf 'Recovery qualification retains its deployment and TLS peer. Original receipts: %q.\n' "$provider_host_root" >&2 || :
        if [[ $inference_retirement_confirmed == true ]]; then docker_client_cleanup || :; exit 2; fi
        while :; do sleep 1 || :; done
    fi
    provider_host_finish "$status"
}

recovery_witness_main() {
    if (( $# != 0 )) || [[ $(id -u) == 0 || $(id -g) == 0 ]]; then
        printf '%s\n' 'Use make test/integration/provider-service/recovery as a nonroot caller; this fixed local fixture accepts no selectors.' >&2
        return 2
    fi
    repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
    source "$repository_root/tools/provider.sh"
    source "$repository_root/tools/provider_api_qualification.sh"
    umask 077
    local scratch fixture namespace config source_root state selector authority print_command=false controller_image path
    scratch=$(mktemp -d /tmp/nmrnet-recovery-service.XXXXXXXX)
    provider_host_root=$scratch/fixture-owner
    provider_host_diagnostic= provider_host_drain_seconds=10 provider_host_recovery_mode=service
    provider_host_initialize || return $?
    foreground_pid= foreground_signalled=false api_deployment_active=false
    trap recovery_witness_finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM HUP
    source "$repository_root/tools/provider_image.sh"
    require_provider_image
    controller_image=$provider_image_id
    local -a source_files=(Makefile tools/provider_recovery_qualification.sh tools/provider.sh
        tools/provider_api_qualification.sh tools/provider_image.sh tools/provider_workers.sh tools/provider_logging.sh
        tools/docker_client.sh tools/inference_native.sh tools/worker_retirement.sh
        requirements/provider-core.lock requirements/provider-core-build.lock .work/provider/image-id
        tools/provider_deployment.sh tools/deployment_helper_grants.sh tools/deployment_runtime_recovery.sh
        tools/provider_deployment.py tools/compose_runner_events.sh
        tests/integration/provider_runtime/recovery_peer.py tests/integration/provider_runtime/stage_api_config.py)
    find "$repository_root/packages/provider-core/tests" "$repository_root/tests/integration/provider_core" \
        "$repository_root/nmrnet_provider" -type f -name '*.py' -print0 > "$scratch/source-paths" || return $?
    sort -z -o "$scratch/source-paths" "$scratch/source-paths" || return $?
    while IFS= read -r -d '' path; do source_files+=("${path#"$repository_root"/}"); done < "$scratch/source-paths"
    (cd -- "$repository_root" && sha256sum -- "${source_files[@]}") > "$scratch/sources-before.sha256"
    repository_git -C "$repository_root" rev-parse --verify HEAD > "$scratch/source-revision"
    printf '%s\n' "$controller_image" > "$scratch/controller-image-id"
    fixture=$scratch/fixture
    mkdir -m 700 -- "$fixture"
    namespace=$scratch/fixture-code
    # Read-only parent mounts cannot create missing child mountpoints. Preserve
    # the existing test-package hierarchy in private public-code scaffolding.
    mkdir -m 700 -- "$namespace" "$namespace/tests" "$namespace/tests/integration" \
        "$namespace/tests/fakes" "$namespace/tests/integration/nmrnet" || return $?
    cp -- "$repository_root/packages/provider-core/tests/__init__.py" "$namespace/tests/" || return $?
    cp -- "$repository_root/packages/provider-core/tests/integration/__init__.py" "$namespace/tests/integration/" || return $?
    cp -- "$repository_root/tests/integration/provider_runtime/recovery_peer.py" "$namespace/recovery_peer.py" || return $?
    cp -- "$repository_root/tests/integration/provider_runtime/stage_api_config.py" "$namespace/stage_api_config.py" || return $?
    cp -- "$repository_root/tests/integration/provider_core/config_ports.py" "$namespace/config_ports.py" || return $?
    if ! cmp -s -- "$repository_root/tests/integration/provider_runtime/recovery_peer.py" "$namespace/recovery_peer.py"; then
        provider_host_refuse 'The private recovery fixture copy differs from its frozen source.'; return 2
    fi
    sha256sum -- "$namespace/tests/__init__.py" "$namespace/tests/integration/__init__.py" "$namespace/recovery_peer.py" \
        "$namespace/stage_api_config.py" "$namespace/config_ports.py" \
        > "$scratch/fixture-code.sha256"
    printf 'Private local recovery evidence: %s\n' "$scratch"
    provider_host_grants=(
        --mount "type=bind,src=$namespace,dst=/fixture,readonly"
        --mount "type=bind,src=$repository_root/packages/provider-core/tests/fakes,dst=/fixture/tests/fakes,readonly"
        --mount "type=bind,src=$repository_root/tests/integration/provider_core,dst=/fixture/tests/integration/nmrnet,readonly"
        --mount "type=bind,src=$fixture,dst=$fixture")
    provider_host_argv=(/opt/provider/environment/bin/python -B /fixture/recovery_peer.py --root "$fixture" --seconds 180)
    provider_host_create api host "$controller_image" provider_host_grants provider_host_argv true || return $?
    recovery_witness_wait_file "$fixture/ready.json" || return $?
    IFS= read -r config < "$fixture/config.txt"
    IFS= read -r source_root < "$fixture/source-root.txt"
    IFS= read -r state < "$fixture/store-root.txt"
    IFS= read -r selector < "$fixture/record-sha256.txt"
    IFS= read -r authority < "$fixture/authority.txt"
    for path in "$config" "$source_root" "$state"; do provider_host_path "$path" || return $?; done
    [[ $selector =~ ^[0-9a-f]{64}$ && $authority =~ ^127\.0\.0\.1:[1-9][0-9]{0,4}$ ]] \
        || { provider_host_refuse 'The local fixture returned an invalid record selector or loopback TLS authority.'; return 2; }
    local -a common=("CONFIG=$config" "SOURCE_ROOT=$source_root" MAXIMUM_STARTUP_BYTES=131072 HOST_DRAIN_SECONDS=10)
    make --no-print-directory -C "$repository_root" provider/recovery/inspect "${common[@]}" "INSTALLATION_ROOT=$scratch/inspect" &
    foreground_pid=$! foreground_signalled=false
    recovery_witness_wait_foreground || return $?
    [[ -f $scratch/inspect/components/controller/diagnostics/result.json ]] || return 2
    : > "$fixture/restore-inputs"
    recovery_witness_wait_file "$fixture/inputs-restored" || return $?
    make --no-print-directory -C "$repository_root" provider/recovery/run "${common[@]}" "INSTALLATION_ROOT=$scratch/recovery" \
        "RECORD_SHA256=$selector" API_ROUTE=mapped MAPPED_INTERFACE=lo MAPPED_ADDRESSES=127.0.0.1=127.0.0.1 &
    foreground_pid=$! foreground_signalled=false
    recovery_witness_wait_foreground || return $?
    : > "$fixture/idle-phase"
    recovery_witness_wait_file "$fixture/recovered" || return $?
    local public private input
    public=$(mktemp -d "$repository_root/config/deployments/qualification-recovery-XXXXXXXX") || return $?
    api_deployment_name=${public##*/}
    printf '%s\n' "$api_deployment_name" > "$scratch/deployment-name" || return $?
    private=$repository_root/secrets/deployments/$api_deployment_name
    mkdir -p -m 700 -- "$repository_root/secrets/deployments" || return $?
    mkdir -m 700 -- "$private" || return $?
    cp -- "$fixture/deployment/"{provider,deployment}.toml "$public/" || return $?
    for input in signing.private.json interpreter.key api-ca.pem; do
        cp -- "$fixture/deployment/secrets/$input" "$private/$input" || return $?
        chmod 600 -- "$private/$input" || return $?
    done
    NMRNET_DEPLOYMENT_INPUT=$api_deployment_name bash "$repository_root/tools/provider_deployment.sh" install || return $?
    api_deployment_active=true
    NMRNET_DEPLOYMENT_INPUT=$api_deployment_name bash "$repository_root/tools/provider_deployment.sh" up || return $?
    recovery_witness_wait_file "$fixture/idled" || return $?
    api_witness_stop_deployment || return $?
    # These are read-only observations of the stack already admitted by up/down.
    # Socket files now persist at idle; absence of old request directories is no
    # longer an allocation witness. The existing event projection checks children.
    local project role identifier
    local -a identifiers=()
    project=$(printf '%s\0%s' "$repository_root" "$api_deployment_name" | sha256sum) || return $?
    project=nmrnet-${project:0:20}
    for role in prediction reader smiles; do
        provider_host_command "$role-id" "$provider_host_root" ps --all --no-trunc --quiet \
            --filter "label=com.docker.compose.project=$project" --filter "label=com.docker.compose.service=$role" || return $?
        mapfile -t identifiers < "$provider_host_root/$role-id.stdout"
        [[ ${#identifiers[@]} == 1 && ${identifiers[0]} =~ ^[0-9a-f]{64}$ ]] || return 2
        identifier=${identifiers[0]}
        inference_native 'idle native-child observation' 30 stdio bash "$repository_root/tools/compose_runner_events.sh" \
            "$identifier" "${engine[@]}" > "$scratch/$role.events" || return $?
        [[ ! -s $scratch/$role.events ]] || { provider_host_refuse 'Recovered-store idle restart started native work despite empty discovery.'; return 2; }
    done
    : > "$fixture/done"
    api_witness_wait api 15 || return $?
    recovery_witness_wait_file "$fixture/summary.json" || return $?
    provider_host_remove api || return $?
    (cd -- "$repository_root" && sha256sum -- "${source_files[@]}") > "$scratch/sources-after.sha256"
    if ! cmp -s -- "$scratch/sources-before.sha256" "$scratch/sources-after.sha256"; then
        provider_host_refuse 'Recovery qualification source inputs changed during the run; preserve the original receipts.'; return 2
    fi
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    set -euo pipefail
    recovery_witness_main "$@"
fi
