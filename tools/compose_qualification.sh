#!/usr/bin/env bash
# Opt-in qualification uses the production recipe owners in a private project.
# The local signed peers own fixture data; no host delivery singleton is changed.

qualification_delivery_prepare() {
    local provider_private=$1 output=$2 project=$3 controller_image=$4 bundle helper_uuid
    [[ $project == nmrnet-qualification-* ]] || {
        printf '%s\n' 'Qualification requires its own delivery project; the host singleton is not a test fixture.' >&2
        return 2
    }
    qualification_delivery_root=$output qualification_delivery_project=$project
    qualification_delivery_prepared=false qualification_delivery_started=false qualification_delivery_helper=
    bundle=$(realpath -e -- "$provider_private/current") || return $?
    source "$repository_root/tools/delivery_image.sh"
    require_delivery_image || return $?
    qualification_delivery_image=$delivery_image_id
    mkdir -m 700 -- "$output" || return $?
    IFS= read -r helper_uuid < /proc/sys/kernel/random/uuid
    qualification_delivery_helper=$project-config-${helper_uuid//-/}
    inference_native 'freeze private qualification delivery' 120 stdio "${engine[@]}" run --rm --pull never \
        --name "$qualification_delivery_helper" --init --network none --read-only \
        --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus 1 --memory 512m --memory-swap 512m --pids-limit 64 \
        --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=32m,mode=0700,uid=$(id -u),gid=$(id -g)" \
        --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 --log-opt mode=non-blocking --log-opt max-buffer-size=1m \
        --mount "type=bind,src=$bundle,dst=$bundle,readonly" --mount "type=bind,src=$output,dst=$output" \
        --entrypoint /opt/provider/environment/bin/python "$controller_image" -I -B \
        -m nmrnet_provider.delivery_deployment prepare --provider-bundle "$bundle" --output "$output" \
        --uid "$(id -u)" --gid "$(id -g)" --delivery-image "$delivery_image_id" \
        --controller-image "$controller_image" --project "$project" || return $?
    qualification_delivery_helper=
    qualification_delivery_prepared=true
}

qualification_delivery_compose() {
    local seconds=$1
    shift
    inference_native 'private qualification delivery Compose' "$seconds" stdio "${engine[@]}" compose \
        --env-file /dev/null --project-directory "$qualification_delivery_root" \
        --project-name "$qualification_delivery_project" --file "$qualification_delivery_root/compose.json" "$@"
}

qualification_delivery_up() {
    # An interrupted up may already have created containers.
    qualification_delivery_started=true
    qualification_delivery_compose 180 up --detach --no-build --pull never --wait --wait-timeout 120
}

qualification_delivery_down() {
    [[ ${qualification_delivery_prepared-false} == true ]] || return 0
    qualification_delivery_compose 120 down --timeout 60 || return $?
    qualification_delivery_started=false
}

qualification_delivery_empty() {
    # The daemon must be stopped for the existing operator's exclusive queue lock.
    # The idle-recovery case may keep its producer alive; deleting volumes also
    # requires stopped producers and independent API-success evidence.
    local helper_uuid
    IFS= read -r helper_uuid < /proc/sys/kernel/random/uuid
    qualification_delivery_helper=$qualification_delivery_project-inspect-${helper_uuid//-/}
    inference_native 'inspect stopped private delivery metadata' 30 stdio "${engine[@]}" run --rm --pull never \
        --name "$qualification_delivery_helper" --init --network none --read-only --user "$(id -u):$(id -g)" \
        --cap-drop ALL --security-opt no-new-privileges:true --cpus 1 --memory 256m --memory-swap 256m --pids-limit 32 \
        --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=0700,uid=$(id -u),gid=$(id -g)" \
        --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 --log-opt mode=non-blocking --log-opt max-buffer-size=1m \
        --mount "type=bind,src=$qualification_delivery_root/daemon.toml,dst=/input/daemon.toml,readonly" \
        --mount "type=volume,src=$qualification_delivery_project-queue,dst=/state,volume-nocopy" \
        --entrypoint /opt/provider-core/environment/bin/python "$qualification_delivery_image" -I -B -c '
import contextlib, io, json, runpy, sys, tomllib
from pathlib import Path
configuration = tomllib.loads(Path("/input/daemon.toml").read_text())
outstanding = 0
for binding in configuration["bindings"]:
    api = binding["api"]
    sys.argv = ["held", "--config", "/input/daemon.toml",
                "--api-origin", api["origin"], "--provider-ref", api["provider_ref"], "--list-held"]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        runpy.run_module("provider_core.delivery_discard", run_name="__main__")
    print(output.getvalue(), end="")
    outstanding += json.loads(output.getvalue())["outstanding_items"]
if outstanding:
    print("Private delivery still owns outcomes; its volumes must remain.", file=sys.stderr)
    raise SystemExit(2)
' || return $?
    qualification_delivery_helper=
}

qualification_delivery_remove() {
    # The caller has confirmed its API witness, native retirement and empty queue.
    qualification_delivery_compose 120 down --timeout 60 --volumes || return $?
    qualification_delivery_prepared=false
}

qualification_delivery_retire_helper() {
    [[ -n ${qualification_delivery_helper-} ]] || return 0
    inference_native 'retire private delivery helper' 15 stdio \
        "${engine[@]}" rm --force "$qualification_delivery_helper" || return $?
    qualification_delivery_helper=
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

compose_witness_role() {
    local identifier
    identifier=$(inference_native 'locate private qualification role' 10 stdio "${engine[@]}" ps --all --no-trunc --quiet \
        --filter "label=io.nmrnet-repro.deployment=$deployment" --filter "label=com.docker.compose.service=$1") || return $?
    [[ $identifier =~ ^[0-9a-f]{64}$ ]] || {
        printf 'Qualification role %s is absent or ambiguous.\n' "$1" >&2
        return 2
    }
    printf '%s\n' "$identifier"
}

compose_witness_provider() {
    NMRNET_DEPLOYMENT_INPUT=$deployment bash "$repository_root/tools/provider_deployment.sh" "$@"
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
        identifier=$(compose_witness_role "$role") || return $?
        while true; do
            projection=$(inference_native 'observe bounded native lifecycle fields' 10 stdio \
                bash "$capture/tools/compose_runner_events.sh" "$identifier" "${engine[@]}") || return $?
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
    identifier=$(compose_witness_role reader) || return $?
    before=$(inference_native 'observe reader before controlled crash' 10 stdio "${engine[@]}" inspect \
        --format '{{.RestartCount}}' "$identifier") || return $?
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
    budget=$((deadline - SECONDS)); (( budget > 0 )) || return 2
    inference_native 'wait for restarted qualification roles' "$budget" stdio \
        env NMRNET_DEPLOYMENT_INPUT="$deployment" bash "$repository_root/tools/provider_deployment.sh" up || return $?
    after=$(inference_native 'observe reader after controlled crash' 10 stdio "${engine[@]}" inspect \
        --format '{{.RestartCount}}' "$identifier") || return $?
    [[ $after =~ ^[1-9][0-9]*$ ]] || return 2
    inference_native 'inspect restarted reader scratch' 10 stdio "${engine[@]}" exec "$identifier" \
        /opt/rdkit/environment/bin/python -I -B -c \
        'import os; assert not os.listdir("/scratch"), "Restarted reader retained old scratch"; print("fresh-root")' || return $?
    printf 'fresh-root\n' > "$peer_data/reader-fresh-root.pending"
    mv -- "$peer_data/reader-fresh-root.pending" "$peer_data/reader-fresh-root"
    budget=$((deadline - SECONDS)); (( budget > 0 )) || return 2
    compose_witness_wait "$peer_data/reader-crash-receipt.json" "$budget"
}

compose_witness_notice() {
    local identifier=$1 event=$2 since=$3 logs deadline=$((SECONDS + 30))
    local needle="\"event\":\"$event\""
    while (( SECONDS < deadline )); do
        logs=$(inference_native 'read bounded qualification recovery notices' 10 stdio \
            "${engine[@]}" logs --since "$since" --tail 80 "$identifier" 2>&1) || return $?
        if [[ $logs == *"$needle"* ]]; then
            printf '%s\n' "$logs"
            return 0
        fi
        sleep 0.1
    done
    printf 'Qualification did not observe %s within its diagnostic window; inspect Docker logs.\n' "$event" >&2
    return 2
}

compose_witness_relay_restart() {
    local identifier before after queue_before queue_after since
    identifier=$(compose_witness_role provider) || return $?
    before=$(inference_native 'observe idle provider before relay restart' 10 stdio "${engine[@]}" inspect \
        --format '{{.Id}}|{{.State.Pid}}|{{.RestartCount}}|{{.State.Running}}' "$identifier") || return $?
    queue_before=$(inference_native 'observe private queue file before restart' 10 stdio "${engine[@]}" exec \
        "$qualification_delivery_project-daemon" stat -c '%d:%i' /state/delivery.sqlite3) || return $?
    since=$(date -u +'%Y-%m-%dT%H:%M:%S.%NZ')
    qualification_delivery_down || return $?
    qualification_delivery_empty || return $?
    compose_witness_notice "$identifier" api_delivery_retry "$since" || return $?
    qualification_delivery_up || return $?
    compose_witness_notice "$identifier" api_delivery_recovered "$since" || return $?
    compose_witness_provider up || return $?
    after=$(inference_native 'observe idle provider after relay restart' 10 stdio "${engine[@]}" inspect \
        --format '{{.Id}}|{{.State.Pid}}|{{.RestartCount}}|{{.State.Running}}' "$identifier") || return $?
    [[ $before == "$after" && $after == *'|0|true' ]] || {
        printf '%s\n' 'The private provider restarted during idle relay recovery.' >&2
        return 2
    }
    queue_after=$(inference_native 'observe private queue file after restart' 10 stdio "${engine[@]}" exec \
        "$qualification_delivery_project-daemon" stat -c '%d:%i' /state/delivery.sqlite3) || return $?
    [[ $queue_before == "$queue_after" ]] || {
        printf '%s\n' 'The private queue file identity changed across delivery restart.' >&2
        return 2
    }
    printf '%s\n' 'Private relay restarted with an empty retained queue; the idle provider process stayed unchanged.'
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
    if [[ ${qualification_delivery_prepared-false} == true ]]; then
        qualification_delivery_down || {
            [[ -z $inference_diagnostic ]] || printf '%s\n' "$inference_diagnostic" >&2 || :
            printf 'Private delivery shutdown is unconfirmed; its fixture peer remains: %s\n' "$scratch" >&2
            if [[ $inference_retirement_confirmed != true ]]; then while :; do sleep 1 || :; done; fi
            exit 2
        }
        if (( status == 0 )); then
            if qualification_delivery_empty; then qualification_delivery_remove || cleanup=$?
            else cleanup=$?; fi
        fi
        [[ -z $inference_diagnostic ]] || printf '%s\n' "$inference_diagnostic" >&2 || cleanup=2
    fi
    if [[ -n ${qualification_delivery_helper-} && $inference_retirement_confirmed == true ]]; then
        qualification_delivery_retire_helper || cleanup=$?
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
    docker_client_directory= pending_diagnostic= inference_diagnostic= worker_retirement_diagnostic=
    inference_retirement_confirmed=true inference_cancellation_status=0
    peer_id= deployment_installed=false peer_creation_attempted=false print_command=false
    qualification_delivery_prepared=false qualification_delivery_started=false qualification_delivery_helper=
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
        tests/integration/provider_core packages/provider-core/tests tools/compose_runner_events.sh \
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
        --mount "type=bind,src=$capture/packages/provider-core/tests,dst=/fixture/core-tests,readonly" \
        --mount "type=bind,src=$capture/tests/integration/provider_core,dst=/fixture/binding-tests,readonly" \
        --mount "type=bind,src=$capture/tests/integration/provider_runtime/compose_peer.py,dst=/fixture/compose_peer.py,readonly" \
        --mount "type=bind,src=$capture/tests/fixtures/preparation/expected.json,dst=/fixture/expected.json,readonly" \
        --mount "type=bind,src=$capture/tests/fixtures/preparation/smiles.json,dst=/fixture/smiles.json,readonly" \
        --mount "type=bind,src=$capture/tests/fixtures/scientific_inputs,dst=/fixture/scientific_inputs,readonly" \
        --mount "type=bind,src=$capture/artifacts/reference-cpu-float32.json,dst=/fixture/reference-cpu-float32.json,readonly" \
        --mount "type=bind,src=$capture/artifacts/prediction-cpu-float32.json,dst=/fixture/prediction-cpu-float32.json,readonly" \
        --entrypoint /opt/provider/environment/bin/python "$provider_image_id" -I -B /fixture/compose_peer.py \
        --root "$peer_data" --seconds 420 --case "$scientific_case" || create_status=$?
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
    compose_witness_provider install --delivery-project "$delivery_project" || return $?
    deployment_installed=true
    qualification_delivery_prepare "$private" "$scratch/delivery" "$delivery_project" "$provider_image_id" || return $?
    qualification_delivery_up || return $?
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
        compose_witness_provider_compose 120 restart --timeout 60 provider || return $?
        compose_witness_provider up || return $?
        compose_witness_native_events after-restart || return $?
    else
        compose_witness_provider down || return $?
        compose_witness_provider up || return $?
    fi
    compose_witness_provider up || return $?
    compose_witness_provider down || return $?
    deployment_installed=false
    : > "$peer_data/done"
    compose_witness_wait "$peer_data/receipt.json" 15 || return $?
    worker_retirement_command wait "$scratch/peer" "${engine[@]}" wait "$peer_id" || return $?
    cmp -s "$scratch/peer/wait.stdout" <(printf '0\n') || return 2
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then compose_witness_main "$@"; fi
