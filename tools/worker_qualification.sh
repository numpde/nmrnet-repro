#!/usr/bin/env bash
# Fixed live-engine witness for note 017; no deployment or scientific entrypoint.
# Host owns Docker. The two contained probes receive disjoint control grants.
# Create admission follows tools/docker.sh at e845c5a; engine retirement delegates
# to worker_retirement.sh. No name, connection close or timeout proves retirement.
set -euo pipefail
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly base_image='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
readonly caller_uid="$(id -u)" caller_gid="$(id -g)"
if [[ $# != 1 || ! $1 =~ ^(success|grant-failure|unstarted)$ ]]; then
    printf '%s\n' 'Use make test/integration/provider-worker for the fixed retirement scenarios.' >&2
    exit 2
fi
readonly scenario=$1
if [[ $caller_uid == 0 || $caller_gid == 0 || $repository_root == *','* || $repository_root == *$'\n'* || $repository_root == *$'\r'* ]]; then
    printf '%s\n' 'Worker qualification requires a nonzero UID/GID and a repository path without commas or line breaks.' >&2
    exit 2
fi
for path in tests tests/integration tests/integration/provider_worker tools; do
    if [[ ! -d $repository_root/$path || -L $repository_root/$path ]]; then
        printf 'Cannot qualify worker retirement: %s must be a source directory without a symlink.\n' "$path" >&2
        exit 2
    fi
done
for path in tests/integration/provider_worker/retirement_probe.py tools/container_guard.py; do
    if [[ ! -f $repository_root/$path || -L $repository_root/$path ]]; then
        printf 'Cannot qualify worker retirement: %s must be a regular source file without a symlink.\n' "$path" >&2
        exit 2
    fi
done
source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
source "$repository_root/tools/worker_retirement.sh"
docker_client_directory=
inference_retirement_confirmed=true
inference_cancellation_status=0
inference_diagnostic=
worker_retirement_diagnostic=
pending_diagnostic=
scratch=
worker_id= controller_id=
worker_attempted=false controller_attempted=false
worker_removed=false controller_removed=false
unresolved_create=false
finished=false

retire_endpoint() {
    local role=$1 identifier=$2 status
    printf -v "${role}_attempted" '%s' true
    if worker_container_retire "$identifier" "$scratch/$role" "${engine[@]}"; then
        printf -v "${role}_removed" '%s' true
        return 0
    else status=$?; fi
    pending_diagnostic+=$worker_retirement_diagnostic
    return "$status"
}

finish() {
    local status=$? cleanup_status=0
    trap - EXIT
    # Do not re-enter an uncertain retirement or replace its native-owner fact.
    if [[ $inference_retirement_confirmed == true && -n $worker_id && $worker_attempted == false ]]; then
        retire_endpoint worker "$worker_id" || cleanup_status=$?
    fi
    if [[ $inference_retirement_confirmed == true && -n $controller_id && $controller_attempted == false ]]; then
        retire_endpoint controller "$controller_id" || cleanup_status=$?
    fi
    if [[ $status == 0 && $cleanup_status != 0 ]]; then status=$cleanup_status; fi
    if [[ $finished == true && $worker_removed == true && ( -z $controller_id || $controller_removed == true ) && $inference_retirement_confirmed == true ]]; then
        if rm -rf -- "$scratch"; then :; else
            cleanup_status=$?
            if [[ $status == 0 ]]; then status=$cleanup_status; fi
            printf -v pending_diagnostic '%sCannot confirm qualification scratch removal at %q (status %s).\n' "$pending_diagnostic" "$scratch" "$cleanup_status"
        fi
    elif [[ -n $scratch ]]; then
        printf -v pending_diagnostic '%sWorker qualification did not finish; preserve private allocation receipts at %q (unresolved create: %s).\n' \
            "$pending_diagnostic" "$scratch" "$unresolved_create"
        if [[ $status == 0 ]]; then status=2; fi
    fi
    if [[ $inference_retirement_confirmed == true ]]; then
        if docker_client_cleanup; then :; else
            cleanup_status=$?
            if [[ $status == 0 ]]; then status=$cleanup_status; fi
        fi
    fi
    if [[ -n $pending_diagnostic ]]; then printf '%s\n' "$pending_diagnostic" >&2; fi
    if [[ $status == 0 && $finished == true ]]; then
        printf 'Worker retirement qualification passed: %s.\n' "$scenario"
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

metadata() {
    local operation=$1 directory=$2 status
    shift 2
    if worker_retirement_command "$operation" "$directory" "${engine[@]}" "$@"; then return 0; else status=$?; fi
    pending_diagnostic+=$worker_retirement_diagnostic
    return "$status"
}

start_endpoint() {
    local role=$1 candidate= create_status=0
    local -a grants=(--mount "type=bind,src=$scratch/$role/diagnostics,dst=/diagnostics")
    if [[ $role == worker ]]; then
        grants+=(--mount "type=bind,src=$scratch/ipc,dst=/ipc")
    else
        grants+=(--mount "type=bind,src=$scratch/ipc,dst=/ipc,readonly"
            --mount "type=bind,src=$scratch/request,dst=/control/request"
            --mount "type=bind,src=$scratch/receipt,dst=/control/receipt,readonly")
    fi
    unresolved_create=true
    if metadata create "$scratch/$role" create --cidfile "$scratch/$role/worker.cid" \
        --init --network none --pull never --read-only --restart no --log-driver none \
        --user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus 1 --memory 128m --memory-swap 128m --pids-limit 32 \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=8m --workdir /workspace \
        --env NMRNET_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 \
        --mount "type=bind,src=$repository_root/tests/integration/provider_worker/retirement_probe.py,dst=/workspace/probe.py,readonly" \
        --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly" \
        "${grants[@]}" "$image_id" bash -eu -c \
        'umask 077; ulimit -f 64; exec python3 -B /workspace/probe.py "$@" > /diagnostics/stdout 2> /diagnostics/stderr' \
        bash "$role" "$allocation" "$scenario"; then :; else create_status=$?; fi
    # Even a failed client can leave a matching CID pair. Admit only complete
    # agreement; otherwise retain uncertainty without starting or deleting by name.
    if [[ $inference_retirement_confirmed == true && -f $scratch/$role/worker.cid && ! -L $scratch/$role/worker.cid ]] \
        && IFS= read -r -N 64 candidate < "$scratch/$role/worker.cid" \
        && [[ $candidate =~ ^[0-9a-f]{64}$ ]] \
        && { cmp -s -- "$scratch/$role/worker.cid" <(printf '%s' "$candidate") \
            || cmp -s -- "$scratch/$role/worker.cid" <(printf '%s\n' "$candidate"); } \
        && cmp -s -- "$scratch/$role/create.stdout" <(printf '%s\n' "$candidate"); then
        printf -v "${role}_id" '%s' "$candidate"
        unresolved_create=false
    else
        pending_diagnostic+='Cannot admit created probe container: private CID and complete create response do not agree. '
        return 2
    fi
    if [[ $create_status != 0 ]]; then return "$create_status"; fi
    if [[ $scenario == unstarted && $role == worker ]]; then return 0; fi
    metadata start "$scratch/$role" start "$candidate"
}

await_bytes() {
    local path=$1 expected=$2 deadline=$((SECONDS + 15))
    while (( SECONDS < deadline )); do
        if [[ -f $path && ! -L $path ]] && cmp -s -- "$path" <(printf '%s\n' "$expected"); then return 0; fi
        sleep 0.05
    done
    pending_diagnostic+='Worker qualification timed out awaiting its exact allocation-bound observation. '
    return 2
}

scratch=$(umask 077; mktemp -d /tmp/nmrnet-worker-proof.XXXXXXXX)
allocation=${scratch##*/}
mkdir -m 700 -- "$scratch/worker" "$scratch/controller" "$scratch/ipc" "$scratch/request" "$scratch/receipt"
mkdir -m 700 -- "$scratch/worker/diagnostics" "$scratch/controller/diagnostics"
if docker_client_prepare run; then :; else pending_diagnostic+=$docker_client_diagnostic; exit 2; fi
metadata image "$scratch" image inspect --format '{{.Id}}' "$base_image"
IFS= read -r image_id < "$scratch/image.stdout"
if [[ ! $image_id =~ ^sha256:[0-9a-f]{64}$ ]] || ! cmp -s -- "$scratch/image.stdout" <(printf '%s\n' "$image_id"); then
    pending_diagnostic+='Cannot admit probe image: inspection did not return one immutable image ID. '
    exit 2
fi
start_endpoint worker
if [[ $scenario == unstarted ]]; then
    retire_endpoint worker "$worker_id"
    finished=true
    exit 0
fi
await_bytes "$scratch/ipc/ready" "$allocation"
metadata running "$scratch/worker" inspect --type container --format '{{.Id}} {{.State.Running}}' "$worker_id"
if ! cmp -s -- "$scratch/worker/running.stdout" <(printf '%s true\n' "$worker_id"); then
    pending_diagnostic+='Cannot qualify retirement: the exact blocked worker was not observed running. '
    exit 2
fi
start_endpoint controller
await_bytes "$scratch/request/retire" "retire $allocation"
retire_endpoint worker "$worker_id"
rm -- "$scratch/ipc/ready"
disposition=retired
if [[ $scenario == grant-failure ]]; then
    # Real rmdir failure: a retained grant entry prevents successful cleanup.
    # This fixture-only fault is removed after both containers have retired.
    printf '%s\n' retained > "$scratch/ipc/retained"
fi
if rmdir -- "$scratch/ipc" 2> "$scratch/grant-cleanup.stderr"; then
    if [[ $scenario != success ]]; then pending_diagnostic+='Expected grant-cleanup fault did not occur. '; exit 2; fi
elif [[ $scenario == grant-failure ]]; then disposition=unconfirmed
else pending_diagnostic+='Cannot confirm worker grant cleanup; inspect private cleanup evidence. '; exit 2
fi
printf '%s %s\n' "$disposition" "$allocation" > "$scratch/receipt/pending"
mv -- "$scratch/receipt/pending" "$scratch/receipt/result"
await_bytes "$scratch/request/observed" "$disposition $allocation"
metadata wait "$scratch/controller" wait "$controller_id"
if ! cmp -s -- "$scratch/controller/wait.stdout" <(printf '0\n'); then
    pending_diagnostic+='Contained controller did not accept the expected host disposition. '
    exit 2
fi
retire_endpoint controller "$controller_id"
finished=true
