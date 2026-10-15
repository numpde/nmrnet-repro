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
readonly image_admission_timeout=30
print_command=false
if [[ $# -gt 0 && "$1" == --print ]]; then print_command=true; shift; fi
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

if [[ "$1" == apply ]]; then
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

source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
docker_client_directory=
# Only this launcher's build/apply clients use this config. Stage acquisition
# remains owned by docker.sh; no native child exists before preparation here.
inference_retirement_confirmed=true
cleanup() {
    local status=$? cleanup_status=0 cancelled=0
    trap - EXIT
    # Quiet removal precedes diagnostics, as in inference_image.sh cleanup.
    # Cancellation during removal must finish it without starting a new report.
    trap 'cancelled=129' HUP
    trap 'cancelled=130' INT
    trap 'cancelled=143' TERM
    if "$inference_retirement_confirmed"; then
        if docker_client_cleanup 2>/dev/null; then :; else
            cleanup_status=$?
            if [[ $status == 0 ]]; then status=$cleanup_status; fi
        fi
    fi
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if [[ $cancelled != 0 ]]; then exit "$cancelled"; fi
    if [[ -n "$docker_client_directory" ]]; then
        if ! "$inference_retirement_confirmed"; then
            printf 'Cannot confirm package native client retirement; private Docker client state retained at %q.\n' "$docker_client_directory" >&2
        else
            # A held native report avoids a full-pipe builtin write blocking
            # cancellation. Its new retirement fact authorizes no later cleanup.
            if inference_native 'package client cleanup reporting' untimed stdio bash -eu -c '
                printf "Cannot confirm package Docker client configuration removal; attempted path %q (status %s).\n" "$1" "$2" >&2
            ' bash "$docker_client_directory" "$cleanup_status"; then :; else
                local report_status=$?
                case "$report_status" in
                    129|130|143) status=$report_status ;;
                    *) if [[ $status == 0 ]]; then status=$report_status; fi ;;
                esac
            fi
        fi
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
if "$print_command"; then docker_client_prepare print; else docker_client_prepare run; fi

if [[ "$1" == stage ]]; then
    # Bind the shared fixed BuildKit policy to its default builder, as in the
    # reviewed inference_image.sh build; ambient builders grant no authority.
    build=("${engine[@]}" build --builder default --quiet --network none --pull=false
        --build-arg "PYTHON_BASE=$python_image" --build-arg "UV_IMAGE=$uv_image"
        --file "$repository_root/containers/packages/Dockerfile.lock" "$repository_root")
    if "$print_command"; then
        run "${build[@]}"
        lock_image='sha256:LOCK_IMAGE_ID'
    else
        # Publish only one byte-exact immutable ID. Malformed quiet-build output
        # was never a supported resolver identity; helper facts stay in parent.
        inference_native 'package resolver image build' untimed image-id "${build[@]}"
        lock_image=$inference_result
        # The build's output is a candidate; independently observe its immutable
        # local identity before publishing, as in inference_image.sh admission.
        inference_native 'package built image admission' "$image_admission_timeout" image-id \
            "${engine[@]}" image inspect --format '{{.Id}}' "$lock_image"
        if [[ "$inference_result" != "$lock_image" ]]; then
            printf '%s\n' 'Cannot record package resolver image: observed local image identity differs from the built image ID.' >&2
            exit 2
        fi
        printf '%s\n' "$lock_image" > "$stage_directory/image-id"
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
if [[ "$1" == stage ]]; then
    "${command[@]}"
elif "$print_command"; then
    run "${command[@]}"
else
    inference_native 'package lock apply' untimed stdio "${command[@]}"
fi
if [[ "$1" == stage ]] && ! "$print_command"; then
    # The separate controller has returned. Hold this fixed stdout publisher
    # so an unread full pipe cannot defer cancellation before config cleanup.
    inference_native 'package stage review reporting' untimed stdio bash -eu -c '
        printf "Review %s, then apply with make packages/lock/apply STAGE=%s\n" "$1" "$2"
    ' bash "$stage_directory" "${stage_directory##*/}"
fi
