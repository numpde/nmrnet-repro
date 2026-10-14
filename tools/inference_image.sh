#!/usr/bin/env bash
# Build from verified local inputs offline; import only the recorded immutable image.
set -euo pipefail

readonly python_image='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly caller_uid="$(id -u)"
readonly caller_gid="$(id -g)"
readonly image_admission_timeout=30
source "$repository_root/tools/inference_native.sh"
client_environment=(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH
    -u DOCKER_AUTH_CONFIG -u DOCKER_CONFIG
    -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY
    -u http_proxy -u https_proxy -u all_proxy -u no_proxy
    -u BUILDX_BUILDER -u BUILDX_CONFIG -u DOCKER_BUILDKIT DOCKER_BUILDKIT=1)
print_command=false
if [[ "${1:-}" == --print ]]; then print_command=true; shift; fi
native_timeout=
if [[ "${1:-}" == --timeout ]]; then
    native_timeout=${2:-}
    if [[ ! "$native_timeout" =~ ^([1-9][0-9]*(\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)$ ]]; then
        printf '%s\n' 'Inference --timeout requires positive finite seconds.' >&2
        exit 2
    fi
    shift 2
fi
if [[ $# != 1 || ! "$1" =~ ^(build|import)$ ]]; then
    printf '%s\n' 'Usage: bash tools/inference_image.sh [--print] [--timeout SECONDS] {build|import}' >&2
    exit 2
fi
run() {
    if "$print_command"; then printf '%q ' "$@"; printf '\n'; else "$@"; fi
}
# These are per-client budgets, not a deadline for the entire workflow.
# --timeout overrides build/import; metadata admission always gets 30 seconds.
# Import should finish promptly (120 seconds); construction gets 900 seconds.
if [[ -z "$native_timeout" ]]; then
    if [[ "$1" == build ]]; then native_timeout=900; else native_timeout=120; fi
fi
trap 'exit 130' INT
trap 'exit 143' TERM
if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
    printf '%s\n' 'Inference lanes require a nonzero host UID and GID.' >&2
    exit 2
fi
if [[ "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
    printf '%s\n' 'Repository path cannot contain commas or line breaks in Docker inputs.' >&2
    exit 2
fi
for directory in "$repository_root/.work" "$repository_root/.work/inference"; do
    if [[ -L "$directory" || ( -e "$directory" && ! -d "$directory" ) ]]; then
        printf 'Inference path must be a directory without a symlink: %s\n' "$directory" >&2
        exit 2
    fi
done
image_record="$repository_root/.work/inference/image-id"
if [[ -L "$image_record" || ( -e "$image_record" && ! -f "$image_record" ) ]]; then
    printf '%s\n' 'Cannot use inference image record: image-id must be a regular file without a symlink.' >&2
    exit 2
fi
client_directory=
temporary_record=
cleanup() {
    local status=$? removal_status
    trap - EXIT
    if [[ -n "$temporary_record" ]]; then
        if rm -f -- "$temporary_record"; then :; else
            removal_status=$?
            printf 'Cannot remove unpublished inference image record; retained at %q.\n' "$temporary_record" >&2
            if [[ $status == 0 ]]; then status=$removal_status; fi
        fi
    fi
    if [[ -n "$client_directory" ]]; then
        if rm -rf -- "$client_directory"; then :; else
            removal_status=$?
            printf 'Cannot remove owned Docker client configuration; retained at %q.\n' "$client_directory" >&2
            if [[ $status == 0 ]]; then status=$removal_status; fi
        fi
    fi
    exit "$status"
}
trap cleanup EXIT
if "$print_command"; then
    docker_config=/tmp/nmrnet-inference-client.PRINT
else
    client_directory=$(umask 077; mktemp -d /tmp/nmrnet-inference-client.XXXXXXXX)
    docker_config=$client_directory
fi
engine=("${client_environment[@]}" docker --config "$docker_config" --host unix:///var/run/docker.sock)
if [[ "$1" == build ]]; then
    if [[ ! -d "$repository_root/requirements" || -L "$repository_root/requirements" ]]; then
        printf '%s\n' 'Cannot build inference image: requirements must be a directory without a symlink.' >&2
        exit 2
    fi
    for input in inference.lock unicore-source.json; do
        if [[ ! -f "$repository_root/requirements/$input" || -L "$repository_root/requirements/$input" ]]; then
            printf 'Cannot build inference image: requirement input must be a regular file without a symlink: %s\n' "$input" >&2
            exit 2
        fi
    done
    for directory in "$repository_root/.work/wheelhouse" "$repository_root/.work/wheelhouse/cpu"; do
        if [[ -L "$directory" || ! -d "$directory" ]]; then
            printf '%s\n' 'Cannot build inference image: verified .work/wheelhouse/cpu directory is missing or a symlink.' >&2
            exit 2
        fi
    done
    receipt="$repository_root/.work/wheelhouse/cpu/.complete"
    if [[ ! -f "$receipt" || -L "$receipt" ]]; then
        printf '%s\n' 'Cannot build inference image: wheelhouse completion receipt is missing or a symlink.' >&2
        exit 2
    fi
    gitlink=$(git -C "$repository_root" ls-files --stage -- NMRNetplusplus)
    read -r mode revision stage path <<< "$gitlink"
    if [[ "$mode" != 160000 || ! "$revision" =~ ^[0-9a-f]{40}$ || "$stage" != 0 \
          || "$(git -C "$repository_root/NMRNetplusplus" rev-parse HEAD)" != "$revision" ]]; then
        printf '%s\n' 'Cannot build inference image: NMRNetplusplus must match its recorded submodule revision.' >&2
        exit 2
    fi
    source_changes="$(git -C "$repository_root/NMRNetplusplus" status --porcelain --untracked-files=all -- uninmr LICENSE)"
    source_untracked="$(git -C "$repository_root/NMRNetplusplus" ls-files --others -- uninmr LICENSE)"
    if [[ -n "$source_changes" || -n "$source_untracked" ]]; then
        printf '%s\n' 'Cannot build inference image: uninmr and LICENSE must contain only unchanged pinned source.' >&2
        exit 2
    fi
    verify=("${engine[@]}" run --rm --network none --pull never --read-only
        --env NMRNET_IN_CONTAINER=1 --user "$caller_uid:$caller_gid"
        --cap-drop ALL --security-opt no-new-privileges:true
        --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --workdir /workspace
        --mount "type=bind,src=$repository_root/tools/wheelhouse.py,dst=/workspace/tools/wheelhouse.py,readonly"
        --mount "type=bind,src=$repository_root/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
        --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
        --mount "type=bind,src=$repository_root/requirements/inference.lock,dst=/input/inference.lock,readonly"
        --mount "type=bind,src=$repository_root/requirements/unicore-source.json,dst=/input/unicore-source.json,readonly"
        --mount "type=bind,src=$repository_root/.work/wheelhouse,dst=/wheelhouse,readonly"
        "$python_image" python3 -B -m tools.wheelhouse check
        --lock /input/inference.lock --source /input/unicore-source.json --output /wheelhouse/cpu)
    if "$print_command"; then run "${verify[@]}"; else
        inference_native 'wheelhouse verification' 120 discard "${verify[@]}"
    fi
    if ! "$print_command"; then
        inference_native 'base image admission' "$image_admission_timeout" image-id \
            "${engine[@]}" image inspect --format '{{.Id}}' "$python_image"
        mkdir -p -- "$repository_root/.work/inference"
    fi
    build=("${engine[@]}" build --builder default --quiet --network none --pull=false
        --build-arg "PYTHON_BASE=$python_image" --build-arg "NMRNET_REVISION=$revision"
        --build-context "wheelhouse=$repository_root/.work/wheelhouse/cpu"
        --file "$repository_root/containers/inference/Dockerfile" "$repository_root")
    if "$print_command"; then
        run "${build[@]}"
    else
        inference_native build "$native_timeout" image-id "${build[@]}"
        image_id=$inference_result
        inference_native 'built image admission' "$image_admission_timeout" image-id \
            "${engine[@]}" image inspect --format '{{.Id}}' "$image_id"
        if [[ "$inference_result" != "$image_id" ]]; then
            printf '%s\n' 'Cannot record inference image: observed local image identity differs from the built image ID.' >&2
            exit 2
        fi
        temporary_record="$(mktemp "$repository_root/.work/inference/image-id.XXXXXXXX")"
        printf '%s\n' "$image_id" > "$temporary_record"
        mv -T -- "$temporary_record" "$image_record"
        temporary_record=
        printf 'Built inference image %s\n' "$image_id"
    fi
else
    if [[ ! -f "$image_record" ]]; then
        printf '%s\n' 'Cannot import inference stack: build the inference image first.' >&2
        exit 2
    fi
    if [[ $(wc -c < "$image_record") != 72 ]] \
        || ! LC_ALL=C grep -a -x -E 'sha256:[0-9a-f]{64}' "$image_record" >/dev/null; then
        printf '%s\n' 'Cannot import inference stack: image-id does not contain an immutable image ID.' >&2
        exit 2
    fi
    read -r image_id < "$image_record"
    if ! "$print_command"; then
        inference_native 'recorded image admission' "$image_admission_timeout" image-id \
            "${engine[@]}" image inspect --format '{{.Id}}' "$image_id"
        if [[ "$inference_result" != "$image_id" ]]; then
            printf '%s\n' 'Cannot import inference stack: observed local image identity differs from image-id.' >&2
            exit 2
        fi
    fi
    command=("${engine[@]}" run --rm --network none --pull never --read-only \
        --env NMRNET_IN_CONTAINER=1 \
        --user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus 2 --memory 2g --memory-swap 2g --pids-limit 64 \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m "$image_id")
    if "$print_command"; then
        run "${command[@]}"
    else
        inference_native import "$native_timeout" discard "${command[@]}"
        printf '%s\n' 'Inference imports passed.'
    fi
fi
