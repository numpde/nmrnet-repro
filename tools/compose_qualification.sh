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

compose_witness_finish() {
    local status=$? cleanup=0
    trap - EXIT
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
    case $scientific_case in pasted|references|capacity) ;; *) return 2 ;; esac
    repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
    source "$repository_root/tools/docker_client.sh"
    source "$repository_root/tools/inference_native.sh"
    source "$repository_root/tools/worker_retirement.sh"
    source "$repository_root/tools/git_metadata.sh"
    source "$repository_root/tools/provider_image.sh"
    docker_client_directory= pending_diagnostic= inference_diagnostic= worker_retirement_diagnostic=
    inference_retirement_confirmed=true inference_cancellation_status=0
    peer_id= deployment_installed=false peer_creation_attempted=false print_command=false
    scratch=$(mktemp -d /tmp/nmrnet-compose-XXXXXXXX)
    deployment=qualification-${scratch##*-}
    public=$repository_root/config/deployments/$deployment
    private=$repository_root/secrets/deployments/$deployment
    mkdir -p -m 700 -- "$public" "$private"
    peer_data=$scratch/data
    mkdir -m 700 -- "$scratch/peer" "$peer_data"
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
        tests/integration/provider_runtime/compose_peer.py tests/integration/provider_core packages/provider-core/tests \
        tests/fixtures/preparation/expected.json tests/fixtures/scientific_inputs \
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
        --mount "type=bind,src=$capture/packages/provider-core/tests,dst=/fixture/core-tests,readonly" \
        --mount "type=bind,src=$capture/tests/integration/provider_core,dst=/fixture/binding-tests,readonly" \
        --mount "type=bind,src=$capture/tests/integration/provider_runtime/compose_peer.py,dst=/fixture/compose_peer.py,readonly" \
        --mount "type=bind,src=$capture/tests/fixtures/preparation/expected.json,dst=/fixture/expected.json,readonly" \
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
    make --no-print-directory -C "$repository_root" provider/deployment/up DEPLOYMENT="$deployment" || return $?
    compose_witness_wait "$peer_data/completed.json" 180 || return $?
    make --no-print-directory -C "$repository_root" provider/deployment/status DEPLOYMENT="$deployment" || return $?
    make --no-print-directory -C "$repository_root" provider/deployment/down DEPLOYMENT="$deployment" || return $?
    make --no-print-directory -C "$repository_root" provider/deployment/up DEPLOYMENT="$deployment" || return $?
    # Repeated up is observe-only, and a restart cannot rediscover completed work.
    make --no-print-directory -C "$repository_root" provider/deployment/up DEPLOYMENT="$deployment" || return $?
    make --no-print-directory -C "$repository_root" provider/deployment/down DEPLOYMENT="$deployment" || return $?
    deployment_installed=false
    : > "$peer_data/done"
    compose_witness_wait "$peer_data/receipt.json" 15 || return $?
    worker_retirement_command wait "$scratch/peer" "${engine[@]}" wait "$peer_id" || return $?
    cmp -s "$scratch/peer/wait.stdout" <(printf '0\n') || return 2
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then compose_witness_main "$@"; fi
