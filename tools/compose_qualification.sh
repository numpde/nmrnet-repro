#!/usr/bin/env bash
# Opt-in qualification uses the production recipe owners in a private project.
# The local signed peers own fixture data; no host delivery singleton is changed.

qualification_shared_tools() {
    qualification_provider_tools=${NMRNET_PROVIDER_TOOLS_INPUT-}
    qualification_shared_image=${NMRNET_SHARED_RUNTIME_IMAGE_INPUT-}
    [[ -n $qualification_provider_tools && $qualification_shared_image =~ ^sha256:[0-9a-f]{64}$ ]] || {
        printf '%s\n' 'Qualification requires explicit PROVIDER_TOOLS=<shared checkout> and SHARED_RUNTIME_IMAGE=<immutable local image ID>.' >&2
        return 2
    }
    qualification_provider_tools=$(realpath -e -- "$qualification_provider_tools") || return $?
    [[ -f $qualification_provider_tools/Makefile ]] || return 2
}

qualification_delivery_command() {
    local action=$1
    shift
    local -a selection
    case $action in
        up|held|discard)
            selection=("SHARED_SETTINGS=$qualification_delivery_definition" "SHARED_RUNTIME_IMAGE=$qualification_shared_image") ;;
        down|status|logs) selection=("PROJECT=$qualification_delivery_project") ;;
        *) return 2 ;;
    esac
    controller_native 'shared delivery qualification lifecycle' untimed stdio \
        env -u MAKEFLAGS -u MFLAGS -u MAKEOVERRIDES make --no-print-directory -s \
        -C "$qualification_provider_tools" "delivery/deployment/$action" \
        "${selection[@]}" "$@"
}

qualification_delivery_select() {
    local output=$1 project=$2 definition=$3
    [[ $project == nmrnet-qualification-* ]] || return 2
    qualification_delivery_output=$output qualification_delivery_project=$project
    qualification_delivery_definition=$definition qualification_delivery_up_attempted=false
    mkdir -m 700 -- "$output" || return $?
    qualification_delivery_selected=true
}

qualification_delivery_up() {
    qualification_delivery_up_attempted=true
    qualification_delivery_command up
}

qualification_delivery_down() {
    [[ ${qualification_delivery_selected-false} == true ]] || return 0
    qualification_delivery_command down || return $?
    qualification_delivery_up_attempted=false
}

qualification_delivery_empty() {
    qualification_delivery_command held "API_ORIGIN=$qualification_api_origin" \
        "PROVIDER_REF=$qualification_provider_ref" > "$qualification_delivery_output/held.json" || return $?
    jq -e 'type == "object" and .outstanding_items == 0' \
        "$qualification_delivery_output/held.json" > /dev/null || {
        printf '%s\n' 'Private delivery is not confirmed empty; retain its configuration and volumes.' >&2
        return 2
    }
}

qualification_delivery_release() {
    qualification_fixture_shared retire
}

qualification_fixture_shared() {
    controller_native 'disposable shared fixture lifecycle' untimed stdio \
        bash "$scratch/source/tools/compose_fixture_shared.sh" "$1" "$scratch" \
        "$qualification_delivery_project" "$qualification_shared_image" "$qualification_provider_tools"
}

qualification_delivery_daemon() {
    local output=$qualification_delivery_output/daemon-id
    inference_native 'query private delivery container' 10 stdio bash -eu -c \
        'output=$1; shift; exec "$@" > "$output"' bash "$output" "${engine[@]}" ps --quiet --no-trunc \
        --filter "label=com.docker.compose.project=$qualification_delivery_project" \
        --filter 'label=com.docker.compose.service=delivery' || return $?
    IFS= read -r qualification_delivery_daemon_id < "$output" || return $?
    [[ $qualification_delivery_daemon_id =~ ^[0-9a-f]{64}$ ]] \
        && cmp -s "$output" <(printf '%s\n' "$qualification_delivery_daemon_id") || return 2
    rm -- "$output"
}

compose_witness_wait() {
    local path=$1 deadline=$((SECONDS + $2))
    while [[ ! -f $path || -L $path ]]; do
        (( SECONDS < deadline )) || {
            printf 'Compose qualification did not receive %s within %ss; no Job was resubmitted.\n' "${path##*/}" "$2" >&2
            return 2
        }
        sleep 0.1
    done
}

compose_witness_capture() {
    local operation=$1 budget=$2
    shift 2
    inference_native "$operation" "$budget" stdio bash -eu -c '
        output=$1; shift
        umask 077
        ulimit -f 64
        exec "$@" > "$output"
    ' bash "$scratch/observation" "$@" || return $?
    [[ -f $scratch/observation && ! -L $scratch/observation && $(stat -c %s -- "$scratch/observation") -lt 65536 ]] || return 2
    qualification_observation=$(< "$scratch/observation")
}

compose_witness_role() {
    compose_witness_capture 'locate private qualification role' 10 "${engine[@]}" ps --all --no-trunc --quiet \
        --filter "label=io.nmrnet-repro.deployment=$deployment" --filter "label=com.docker.compose.service=$1" || return $?
    [[ $qualification_observation =~ ^[0-9a-f]{64}$ ]] && cmp -s "$scratch/observation" <(printf '%s\n' "$qualification_observation") || {
        printf 'Qualification role %s is absent or ambiguous.\n' "$1" >&2
        return 2
    }
    qualification_role=$qualification_observation
}

compose_witness_provider() {
    NMRNET_DEPLOYMENT_INPUT=$deployment \
    NMRNET_SHARED_RUNTIME_IMAGE_INPUT=$qualification_shared_image \
        bash "$repository_root/tools/provider_deployment.sh" "$@"
}

compose_witness_provider_compose() {
    local seconds=$1
    shift
    inference_native 'private provider qualification Compose' "$seconds" stdio "${engine[@]}" compose \
        --env-file /dev/null --project-directory "$repository_root" --file "$private/current/compose.json" "$@"
}

compose_witness_native_events() {
    local stage=$1 role identifier projection event operation value reason deadline=$((SECONDS + 15))
    local -A starts=() retirements=()
    for role in prediction reader smiles; do
        compose_witness_role "$role" || return $?
        identifier=$qualification_role
        while true; do
            compose_witness_capture 'observe bounded native lifecycle fields' 10 \
                bash "$capture/tools/compose_runner_events.sh" "$identifier" "${engine[@]}" || return $?
            projection=$qualification_observation
            starts=() retirements=()
            while IFS=$'\t' read -r event operation value reason; do
                [[ -n $event ]] || continue
                case $event in
                    started) [[ ! -v starts[$operation] ]] || return 2; starts[$operation]=$value ;;
                    retired) [[ ! -v retirements[$operation] ]] || return 2; retirements[$operation]=$reason ;;
                    *) return 2 ;;
                esac
            done <<< "$projection"
            if [[ $role == smiles ]]; then [[ ${#starts[@]} == 0 && ${#retirements[@]} == 0 ]] || return 2; break; fi
            (( ${#starts[@]} <= 2 && ${#retirements[@]} <= 2 )) || return 2
            if (( ${#starts[@]} == 2 && ${#retirements[@]} == 2 )); then
                for operation in "${!starts[@]}"; do [[ -v retirements[$operation] ]] || return 2; done
                break
            fi
            (( SECONDS < deadline )) || {
                printf '%s did not expose two retired children within the qualification allowance.\n' "$role" >&2
                return 2
            }
            sleep 0.1
        done
        if [[ $stage == before-restart ]]; then
            native_ids[$role]=$identifier native_events[$role]=$projection
        else
            [[ $identifier == "${native_ids[$role]}" && $projection == "${native_events[$role]}" ]] || return 2
        fi
    done
}

compose_witness_reader_crash() {
    local identifier before after operation program budget status=0 deadline=$((SECONDS + 60))
    compose_witness_role reader || return $?
    identifier=$qualification_role
    compose_witness_capture 'observe reader before controlled crash' 10 "${engine[@]}" inspect \
        --format '{{.RestartCount}}' "$identifier" || return $?
    before=$qualification_observation
    [[ $before == 0 ]] || return 2
    : > "$peer_data/reader-crash-start"
    compose_witness_wait "$peer_data/reader-crash-operation" 15 || return $?
    IFS= read -r operation < "$peer_data/reader-crash-operation" || return 2
    [[ $operation =~ ^[0-9a-f]{32}$ ]] || return 2
    program=$(< "$capture/tests/integration/provider_runtime/runner_crash_probe.py")
    # Killing the supervisor may also kill this exec. The peer's original pidfd,
    # not the exec exit status or a broken socket, establishes old-child exit.
    inference_native 'controlled reader supervisor crash' 20 stdio "${engine[@]}" exec "$identifier" \
        /opt/rdkit/environment/bin/python -I -B -c "$program" "$operation" 15 || status=$?
    [[ $inference_retirement_confirmed == true && $inference_cancellation_status == 0 ]] || return 2
    if (( status != 0 )); then printf 'Controlled crash exec exited %s; awaiting the independent old-child observer.\n' "$status" >&2; fi
    budget=$((deadline - SECONDS)); (( budget > 0 )) || return 2
    compose_witness_wait "$peer_data/reader-old-child-exited.json" "$budget" || return $?
    while :; do
        compose_witness_capture 'observe restarted reader health' 10 "${engine[@]}" inspect \
            --format '{{.RestartCount}}|{{.State.Running}}|{{.State.Health.Status}}' "$identifier" || return $?
        after=$qualification_observation
        [[ $after =~ ^[1-9][0-9]*\|true\|healthy$ ]] && break
        (( SECONDS < deadline )) || { printf '%s\n' 'Restarted reader did not become healthy.' >&2; return 2; }
        sleep 0.1
    done
    inference_native 'inspect restarted reader scratch' 10 stdio "${engine[@]}" exec "$identifier" \
        /opt/rdkit/environment/bin/python -I -B -c \
        'import os; assert not os.listdir("/scratch"), "Restarted reader retained old scratch"; print("fresh-root")' || return $?
    printf 'fresh-root\n' > "$peer_data/reader-fresh-root.pending"
    mv -- "$peer_data/reader-fresh-root.pending" "$peer_data/reader-fresh-root"
    budget=$((deadline - SECONDS)); (( budget > 0 )) || return 2
    compose_witness_wait "$peer_data/reader-crash-receipt.json" "$budget"
}

compose_witness_relay_restart() {
    local identifier before after queue_before queue_after discovery_before discovery_after deadline
    compose_witness_role provider || return $?
    identifier=$qualification_role
    compose_witness_capture 'observe idle provider before relay restart' 10 "${engine[@]}" inspect \
        --format '{{.Id}}|{{.State.Pid}}|{{.RestartCount}}|{{.State.Running}}' "$identifier" || return $?
    before=$qualification_observation
    qualification_delivery_daemon || return $?
    compose_witness_capture 'observe private queue file before restart' 10 "${engine[@]}" exec \
        "$qualification_delivery_daemon_id" stat -c '%d:%i' /state/delivery.sqlite3 || return $?
    queue_before=$qualification_observation
    qualification_delivery_down || return $?
    qualification_delivery_empty || return $?
    compose_witness_wait "$peer_data/discoveries.json" 30 || return $?
    discovery_before=$(jq -er '.count | select(type == "number" and . >= 1 and floor == .)' "$peer_data/discoveries.json") || return $?
    deadline=$((SECONDS + 30))
    while :; do
        discovery_after=$(jq -er '.count | select(type == "number" and . >= 1 and floor == .)' "$peer_data/discoveries.json") || return $?
        (( discovery_after > discovery_before )) && break
        (( SECONDS < deadline )) || { printf '%s\n' 'Signed API discovery did not continue while the outbox was stopped.' >&2; return 2; }
        sleep 0.1
    done
    qualification_delivery_up || return $?
    compose_witness_capture 'observe idle provider after relay restart' 10 "${engine[@]}" inspect \
        --format '{{.Id}}|{{.State.Pid}}|{{.RestartCount}}|{{.State.Running}}' "$identifier" || return $?
    after=$qualification_observation
    [[ $before == "$after" && $after == *'|0|true' ]] || {
        printf '%s\n' 'The private provider restarted during idle relay recovery.' >&2
        return 2
    }
    qualification_delivery_daemon || return $?
    compose_witness_capture 'observe private queue file after restart' 10 "${engine[@]}" exec \
        "$qualification_delivery_daemon_id" stat -c '%d:%i' /state/delivery.sqlite3 || return $?
    queue_after=$qualification_observation
    [[ $queue_before == "$queue_after" ]] || {
        printf '%s\n' 'The private queue file identity changed across delivery restart.' >&2
        return 2
    }
    printf '%s\n' 'Signed API discovery continued while the idle outbox was stopped; the queue file and provider process identities stayed unchanged across restart.'
}

compose_witness_finish() {
    local status=$? cleanup=0
    trap - EXIT
    [[ -z $inference_diagnostic ]] || printf '%s\n' "$inference_diagnostic" >&2 || cleanup=2
    [[ -z $pending_diagnostic ]] || printf '%s\n' "$pending_diagnostic" >&2 || cleanup=2
    pending_diagnostic=
    if [[ $inference_retirement_confirmed != true ]]; then
        printf 'Qualification retains its private peer/configuration because the original Engine client has not retired: %s\n' "$scratch" >&2
        while :; do sleep 1 || :; done
    fi
    if [[ $deployment_installed == true ]]; then
        compose_witness_provider down || {
            [[ -z $inference_diagnostic ]] || printf '%s\n' "$inference_diagnostic" >&2 || :
            printf 'Private deployment shutdown is unconfirmed; its peer and delivery dependencies remain: %s\n' "$scratch" >&2
            if [[ $inference_retirement_confirmed != true ]]; then while :; do sleep 1 || :; done; fi
            exit 2
        }
    fi
    if [[ ${qualification_delivery_selected-false} == true ]]; then
        qualification_delivery_down || {
            [[ -z $inference_diagnostic ]] || printf '%s\n' "$inference_diagnostic" >&2 || :
            printf 'Private delivery shutdown is unconfirmed; its fixture peer remains: %s\n' "$scratch" >&2
            if [[ $inference_retirement_confirmed != true ]]; then while :; do sleep 1 || :; done; fi
            exit 2
        }
        if (( status == 0 )); then
            if ! qualification_delivery_empty || ! qualification_delivery_release; then
                [[ -z $worker_retirement_diagnostic ]] || printf '%s\n' "$worker_retirement_diagnostic" >&2
                printf 'Disposable shared owners remain unresolved; retain their peer, settings and receipts: %s\n' "$scratch" >&2
                if [[ $inference_retirement_confirmed != true ]]; then while :; do sleep 1 || :; done; fi
                exit 2
            fi
        else
            printf 'Failed qualification retains its disposable proxy, peer, queue and settings: %s\n' "$scratch" >&2
            exit "$status"
        fi
        [[ -z $inference_diagnostic ]] || printf '%s\n' "$inference_diagnostic" >&2 || cleanup=2
    fi
    if [[ -n $peer_id && $inference_retirement_confirmed == true ]]; then
        if (( status != 0 )); then
            inference_native 'failed qualification peer diagnostics' 10 stdio "${engine[@]}" logs --tail 80 "$peer_id" || cleanup=$?
            [[ -z $inference_diagnostic ]] || printf '%s\n' "$inference_diagnostic" >&2 || cleanup=2
        fi
        if [[ $inference_retirement_confirmed == true ]]; then
            worker_container_retire "$peer_id" "$scratch/peer" "${engine[@]}" || cleanup=$?
            pending_diagnostic+=$worker_retirement_diagnostic
        fi
    elif [[ -z $peer_id && $peer_creation_attempted == true ]]; then
        printf 'Peer creation has no admitted cleanup handle; preserve its original qualification evidence: %s\n' "$scratch/peer" >&2
        cleanup=2
    fi
    [[ -z $pending_diagnostic ]] || printf '%s\n' "$pending_diagnostic" >&2
    if [[ $inference_retirement_confirmed != true ]]; then
        printf 'Qualification still owns an unretired local client; retain this host process and its original evidence: %s\n' "$scratch" >&2 || :
        while :; do sleep 1 || :; done
    fi
    docker_client_cleanup || cleanup=$?
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
    qualification_shared_tools || return $?
    source "$repository_root/tools/wheelhouse_path.sh"
    select_wheelhouse && require_wheelhouse_pool || return $?
    docker_client_directory= pending_diagnostic= inference_diagnostic= worker_retirement_diagnostic=
    inference_retirement_confirmed=true inference_cancellation_status=0
    peer_id= deployment_installed=false peer_creation_attempted=false print_command=false
    qualification_delivery_selected=false qualification_delivery_up_attempted=false
    scratch=$(mktemp -d /tmp/nmrnet-compose-XXXXXXXX)
    deployment=qualification-${scratch##*-}
    public=$repository_root/config/deployments/$deployment
    private=$repository_root/secrets/deployments/$deployment
    local digest
    digest=$(printf '%s' "$scratch" | sha256sum)
    local delivery_project=nmrnet-qualification-${digest:0:24}
    mkdir -p -m 700 -- "$public" "$private"
    peer_data=$scratch/data
    mkdir -m 700 -- "$scratch/peer" "$peer_data"
    local -a crash_grants=()
    declare -gA native_ids=() native_events=()
    if [[ $scientific_case == recovery ]]; then
        mkdir -m 700 -- "$private/runtime" "$private/runtime/reader" || return $?
        crash_grants=(--mount "type=bind,src=$private/runtime/reader,dst=/reader-witness")
    fi
    trap compose_witness_finish EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    docker_client_prepare run || { pending_diagnostic+=$docker_client_diagnostic; return 2; }
    require_provider_image || return $?
    local capture=$scratch/source revision
    revision=$(repository_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}') || return $?
    printf '%s\n' "$revision" > "$scratch/source-revision"
    source "$repository_root/tools/git_inputs.sh"
    copy_git_blobs 'Compose qualification fixtures' "$repository_root" "$revision" "$capture" "$scratch/fixture-inputs" \
        tests/integration/provider_runtime/compose_peer.py tests/integration/provider_runtime/runner_crash_probe.py \
        tests/integration/provider_core tools/compose_runner_events.sh tools/compose_fixture_shared.sh tools/provider_consumer_tests.py \
        requirements/provider-core-release.json tests/integration/provider_runtime/binding_inputs.py \
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
        --mount "type=bind,src=$peer_data,dst=$peer_data" "${crash_grants[@]}" \
        --env PROVIDER_CORE_IN_CONTAINER=1 --tmpfs /fixture:rw,nosuid,nodev,noexec,size=16m \
        --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" \
        --mount "type=bind,src=$capture/requirements/provider-core-release.json,dst=/fixture/core-release.json,readonly" \
        --mount "type=bind,src=$capture/tools/provider_consumer_tests.py,dst=/fixture/support.py,readonly" \
        --mount "type=bind,src=$capture/tests/integration/provider_runtime/binding_inputs.py,dst=/fixture/binding_inputs.py,readonly" \
        --mount "type=bind,src=$capture/tests/integration/provider_core,dst=/fixture/binding-tests,readonly" \
        --mount "type=bind,src=$capture/tests/integration/provider_runtime/compose_peer.py,dst=/fixture/compose_peer.py,readonly" \
        --mount "type=bind,src=$capture/tests/fixtures/preparation/expected.json,dst=/fixture/expected.json,readonly" \
        --mount "type=bind,src=$capture/tests/fixtures/preparation/smiles.json,dst=/fixture/smiles.json,readonly" \
        --mount "type=bind,src=$capture/tests/fixtures/scientific_inputs,dst=/fixture/scientific_inputs,readonly" \
        --mount "type=bind,src=$capture/artifacts/reference-cpu-float32.json,dst=/fixture/reference-cpu-float32.json,readonly" \
        --mount "type=bind,src=$capture/artifacts/prediction-cpu-float32.json,dst=/fixture/prediction-cpu-float32.json,readonly" \
        --entrypoint /opt/provider/environment/bin/python "$provider_image_id" -I -B /fixture/support.py fixture \
        --release /fixture/core-release.json --pool /wheelhouse --target /fixture --script /fixture/compose_peer.py -- \
        --root "$peer_data" --seconds 420 --case "$scientific_case" --delivery-project "$delivery_project" || create_status=$?
    pending_diagnostic+=$worker_retirement_diagnostic
    worker_container_created "$scratch/peer" || { pending_diagnostic+=$worker_retirement_diagnostic; return 2; }
    peer_id=$worker_created_id
    (( create_status == 0 )) || return "$create_status"
    worker_retirement_command start "$scratch/peer" "${engine[@]}" start "$peer_id" || return $?
    compose_witness_wait "$peer_data/ready.json" 15 || return $?
    cp -- "$peer_data/provider.toml" "$peer_data/deployment.toml" "$public/" || return $?
    for input in api.toml signing.private.json api-ca.pem interpreter.key model-ca.pem store-ca.pem; do
        cp -- "$peer_data/configuration/$input" "$private/$input" || return $?
        chmod 600 -- "$private/$input" || return $?
    done
    qualification_api_origin=$(jq -er '.api_origin' "$peer_data/ready.json") || return $?
    qualification_provider_ref=$(jq -er '.provider_ref' "$peer_data/ready.json") || return $?
    compose_witness_provider install || return $?
    deployment_installed=true
    qualification_delivery_select "$scratch/delivery" "$delivery_project" "$peer_data/shared-settings/settings.toml" || return $?
    if ! qualification_fixture_shared preflight; then
        qualification_delivery_selected=false
        return 2
    fi
    qualification_delivery_up || return $?
    qualification_fixture_shared capture || return $?
    compose_witness_provider up || return $?
    if [[ $scientific_case == recovery ]]; then
        compose_witness_relay_restart || return $?
        compose_witness_reader_crash || return $?
    else
        compose_witness_wait "$peer_data/completed.json" 180 || return $?
    fi
    compose_witness_provider status || return $?
    if [[ $scientific_case == successive ]]; then
        # Restart only the producer: this case explicitly measures warm native reuse.
        compose_witness_native_events before-restart || return $?
        compose_witness_role provider || return $?
        local restarted_provider=$qualification_role deadline
        compose_witness_provider_compose 120 restart --timeout 60 provider || return $?
        deadline=$((SECONDS + 60))
        while :; do
            compose_witness_capture 'observe restarted provider health' 10 "${engine[@]}" inspect \
                --format '{{.State.Running}}|{{.State.Health.Status}}' "$restarted_provider" || return $?
            [[ $qualification_observation == 'true|healthy' ]] && break
            (( SECONDS < deadline )) || { printf '%s\n' 'Restarted provider did not become healthy.' >&2; return 2; }
            sleep 0.1
        done
        compose_witness_native_events after-restart || return $?
    else
        compose_witness_provider down || return $?
        compose_witness_provider up || return $?
    fi
    compose_witness_provider down || return $?
    deployment_installed=false
    : > "$peer_data/done"
    compose_witness_wait "$peer_data/receipt.json" 15 || return $?
    worker_retirement_command wait "$scratch/peer" "${engine[@]}" wait "$peer_id" || return $?
    cmp -s "$scratch/peer/wait.stdout" <(printf '0\n') || return 2
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then compose_witness_main "$@"; fi
