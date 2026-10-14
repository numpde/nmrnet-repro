#!/usr/bin/env bash
# Lock resolution has network and staging writes; explicit application is offline.
# SECS supplies the pinned resolver-image and staged hash-lock pattern:
# https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/make/packages.mk
# https://github.com/numpde/secs-repro/tree/34f5915c1534278bbf61b4c6592afa44cf94ab08/containers/packages
# Separate offline application and advisory receipts are local requirements.
set -euo pipefail

readonly python_image='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
readonly uv_image='ghcr.io/astral-sh/uv@sha256:440fd6477af86a2f1b38080c539f1672cd22acb1b1a47e321dba5158ab08864d'
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly caller_uid="$(id -u)"
readonly caller_gid="$(id -g)"
engine=(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH
    docker --host unix:///var/run/docker.sock)
print_command=false
if [[ "${1:-}" == --print ]]; then print_command=true; shift; fi
if [[ $# != 1 || ! "$1" =~ ^(pull|stage|apply)$ ]]; then
    printf '%s\n' 'Usage: bash tools/packages.sh [--print] {pull|stage|apply}; select apply inputs with make packages/lock/apply STAGE=lock.<id>.' >&2
    exit 2
fi
run() {
    if "$print_command"; then printf '%q ' "$@"; printf '\n'; else "$@"; fi
}
if [[ "$1" == pull ]]; then
    printf '%s\n' 'Base images must be preloaded: Docker daemon pulls cannot be bound by this launcher to WLAN_INTERFACE.' >&2
    printf 'Provision these pinned images through an independently interface-bound process:\n%s\n%s\n' "$python_image" "$uv_image" >&2
    exit 2
fi
if [[ "$1" == stage ]]; then
    bash "$repository_root/tools/docker.sh" check-interface
    if ! "$print_command"; then
        bash "$repository_root/tools/docker.sh" require-images "$python_image" "$uv_image"
    fi
fi
if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
    printf '%s\n' 'Package lanes require a nonzero host UID and GID.' >&2
    exit 2
fi
if [[ "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
    printf '%s\n' 'Repository path cannot contain commas or line breaks in Docker bind mounts.' >&2
    exit 2
fi
for directory in "$repository_root/.work" "$repository_root/.work/packages-stage" "$repository_root/requirements"; do
    if [[ -L "$directory" || ( -e "$directory" && ! -d "$directory" ) ]]; then
        printf 'Package path must be a directory without a symlink: %s\n' "$directory" >&2
        exit 2
    fi
done
for name in inference.in build.in unicore-source.json; do
    if [[ ! -f "$repository_root/requirements/$name" || -L "$repository_root/requirements/$name" ]]; then
        printf 'Package input must be a regular file without a symlink: %s\n' "$name" >&2
        exit 2
    fi
done

if [[ "$1" == apply ]]; then
    # The reviewed stage is a public Make command-line choice, carried as a
    # literal alias rather than accepting an ambient STAGE environment value.
    if [[ ! -v NMRNET_STAGE_INPUT ]] || [[ ! "$NMRNET_STAGE_INPUT" =~ ^lock\.[A-Za-z0-9]+$ ]]; then
        printf '%s\n' 'Select the reviewed lock.<id> with make packages/lock/apply STAGE=lock.<id>.' >&2
        exit 2
    fi
    stage_directory="$repository_root/.work/packages-stage/$NMRNET_STAGE_INPUT"
    if [[ ! -d "$stage_directory" || -L "$stage_directory" ]]; then
        printf 'Cannot apply package stage; missing directory or symlink: %s\n' "$stage_directory" >&2
        exit 2
    fi
else
    stage_directory="$repository_root/.work/packages-stage/lock.PRINT"
    if ! "$print_command"; then
        mkdir -p -- "$repository_root/.work/packages-stage"
        stage_directory="$(mktemp -d "$repository_root/.work/packages-stage/lock.XXXXXXXX")"
    fi
fi

if [[ "$1" == stage ]]; then
    build=("${engine[@]}" build --quiet --network none --pull=false
        --build-arg "PYTHON_BASE=$python_image" --build-arg "UV_IMAGE=$uv_image"
        --file "$repository_root/containers/packages/Dockerfile.lock" "$repository_root")
    if "$print_command"; then
        run "${build[@]}"
        lock_image='sha256:LOCK_IMAGE_ID'
    else
        lock_image="$("${build[@]}")"
        printf '%s\n' "$lock_image" > "$stage_directory/image-id"
    fi
else
    if [[ ! -f "$stage_directory/image-id" || -L "$stage_directory/image-id" ]]; then
        printf '%s\n' 'Cannot apply package stage: its local resolver image ID is missing or a symlink.' >&2
        exit 2
    fi
    read -r lock_image < "$stage_directory/image-id"
    if [[ ! "$lock_image" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        printf '%s\n' 'Cannot apply package stage: invalid local resolver image ID.' >&2
        exit 2
    fi
fi
if [[ "$1" == stage ]]; then
    command=(bash "$repository_root/tools/docker.sh")
    if "$print_command"; then command+=(--print); fi
    command+=(run)
else
    command=("${engine[@]}" run --rm --network none)
fi
# Reap orphaned tool children inside the worker, as in the contained test lanes.
command+=(--init --pull never --read-only --env NMRNET_IN_CONTAINER=1
    --user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 2 --memory 2g --memory-swap 2g --pids-limit 64
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=1g
    --env UV_CACHE_DIR=/tmp/uv --env UV_NO_PROGRESS=1
    --env UV_PYTHON_DOWNLOADS=never)
for name in inference.in build.in unicore-source.json; do
    command+=(--mount "type=bind,src=$repository_root/requirements/$name,dst=/input/$name,readonly")
done
if [[ "$1" == stage ]]; then
    command+=(--mount "type=bind,src=$stage_directory,dst=/output")
else
    # Offline application mounts the reviewed tool and shared guard as modules.
    command+=(--mount "type=bind,src=$stage_directory,dst=/stage,readonly"
        --mount "type=bind,src=$repository_root/requirements,dst=/output"
        --workdir /workspace
        --mount "type=bind,src=$repository_root/tools/package_lock.py,dst=/workspace/tools/package_lock.py,readonly"
        --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly")
fi
if [[ "$1" == stage ]]; then
    command+=("$lock_image" stage)
else
    command+=("$python_image" python3 -B -m tools.package_lock apply)
fi
if [[ "$1" == stage ]]; then "${command[@]}"; else run "${command[@]}"; fi
if [[ "$1" == stage ]] && ! "$print_command"; then
    printf 'Review %s, then apply with make packages/lock/apply STAGE=%s\n' "$stage_directory" "${stage_directory##*/}"
fi
