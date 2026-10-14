#!/usr/bin/env bash
# Explicit dependency acquisition and offline, captured checker-image builds.
# Magnet separates dependency preparation from ordinary check execution:
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/Makefile#L387-L425
set -euo pipefail

readonly base='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly caller_uid="$(id -u)" caller_gid="$(id -g)"
engine=(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH
    -u BUILDX_BUILDER -u BUILDX_CONFIG -u DOCKER_BUILDKIT DOCKER_BUILDKIT=1
    docker --host unix:///var/run/docker.sock)
print_command=false
if [[ $# -gt 0 && "$1" == --print ]]; then print_command=true; shift; fi
if [[ $# -ne 1 || ! "$1" =~ ^(download|check|build)$ ]]; then
    printf '%s\n' 'Usage: bash tools/checker.sh [--print] {download|check|build}' >&2
    exit 2
fi
if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
    printf '%s\n' 'Checker preparation requires a nonzero caller UID and GID.' >&2
    exit 2
fi
if [[ "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
    printf '%s\n' 'Checker preparation cannot use a repository path containing commas or line breaks.' >&2
    exit 2
fi
for directory in "$repository_root/.work" "$repository_root/.work/checker"; do
    if [[ -L "$directory" || ( -e "$directory" && ( ! -d "$directory" || "$(stat -c '%u' "$directory")" != "$caller_uid" ) ) ]]; then
        printf '%s\n' 'Checker preparation requires owned directories without symlinks.' >&2
        exit 2
    fi
done
for directory in tools requirements containers containers/checker; do
    if [[ ! -d "$repository_root/$directory" || -L "$repository_root/$directory" ]]; then
        printf '%s\n' 'Checker source inputs require regular parent directories without symlinks.' >&2
        exit 2
    fi
done
readonly declaration="$repository_root/requirements/checker-dependencies.json"
if [[ ! -f "$declaration" || -L "$declaration" ]]; then
    printf '%s\n' 'Checker preparation requires the reviewed requirements/checker-dependencies.json declaration.' >&2
    exit 2
fi
readonly dependencies="$repository_root/.work/checker/dependencies"
run() {
    if "$print_command"; then printf '%q ' "$@"; printf '\n'; else "$@"; fi
}
worker=(--pull never --read-only --user "$caller_uid:$caller_gid" --cap-drop ALL
    --security-opt no-new-privileges:true --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --env NMRNET_IN_CONTAINER=1 --workdir /workspace)
tool_mounts=(--mount "type=bind,src=$repository_root/tools/checker.py,dst=/workspace/tools/checker.py,readonly"
    --mount "type=bind,src=$repository_root/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
    --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
    --mount "type=bind,src=$declaration,dst=/input/dependencies.json,readonly")
if [[ "$1" == download ]]; then
    bash "$repository_root/tools/docker.sh" check-interface
    if ! "$print_command"; then mkdir -p -m 700 -- "$repository_root/.work/checker"; fi
    proxy_command=(bash "$repository_root/tools/docker.sh")
    if "$print_command"; then proxy_command+=(--print); fi
    download_command=("${proxy_command[@]}" run "${worker[@]}" "${tool_mounts[@]}"
        --mount "type=bind,src=$repository_root/.work/checker,dst=/output"
        "$base" python3 -B -m tools.checker download --declaration /input/dependencies.json
        --directory /output/dependencies --base "$base")
    "${download_command[@]}"
    exit 0
fi
if [[ ! -d "$dependencies" || -L "$dependencies" ]]; then
    printf '%s\n' 'Checker preparation requires acquired dependencies; run the explicit interface-bound download target first.' >&2
    exit 2
fi
verify=("${engine[@]}" run --rm --network none "${worker[@]}" "${tool_mounts[@]}"
    --mount "type=bind,src=$dependencies,dst=/dependencies,readonly"
    "$base" python3 -B -m tools.checker check --declaration /input/dependencies.json
    --directory /dependencies --base "$base")
if [[ "$1" == check ]]; then run "${verify[@]}"; exit; fi
if ! "$print_command"; then
    "${verify[@]}" >/dev/null
    mkdir -p -- "$repository_root/.work/checker"
    capture="$(mktemp -d "$repository_root/.work/checker/.build.XXXXXXXX")"
else
    capture="$repository_root/.work/checker/.build.PRINT"
fi
temporary_record=
cleanup() {
    local status=$? removal_status
    trap - EXIT
    if ! "$print_command"; then
        # Cleanup must report retained state without replacing a primary failure.
        if [[ -n "$temporary_record" ]]; then
            if rm -f -- "$temporary_record"; then :; else
                removal_status=$?
                printf 'Cannot remove unpublished checker image record; retained at %q (removal status %s).\n' "$temporary_record" "$removal_status" >&2
                if [[ "$status" == 0 ]]; then status=$removal_status; fi
            fi
        fi
        if rm -rf -- "$capture"; then :; else
            removal_status=$?
            printf 'Cannot remove checker build capture; retained at %q (removal status %s).\n' "$capture" "$removal_status" >&2
            if [[ "$status" == 0 ]]; then status=$removal_status; fi
        fi
    fi
    exit "$status"
}
trap cleanup EXIT
inputs=(containers/checker/Dockerfile containers/checker/Dockerfile.dockerignore
    tools/checker.py tools/checker.sh tools/artifacts.py tools/container_guard.py requirements/checker-dependencies.json)
if ! "$print_command"; then
    for input in "${inputs[@]}"; do
        [[ -f "$repository_root/$input" && ! -L "$repository_root/$input" ]] || {
            printf '%s\n' 'Checker build inputs must be regular files without symlinks.' >&2; exit 2;
        }
        mkdir -p -- "$capture/context/$(dirname -- "$input")"
        cp --preserve=mode -- "$repository_root/$input" "$capture/context/$input"
    done
    cp -a -- "$dependencies" "$capture/dependencies"
    captured_verify=("${engine[@]}" run --rm --network none "${worker[@]}")
    for tool in checker.py artifacts.py container_guard.py; do
        captured_verify+=(--mount "type=bind,src=$capture/context/tools/$tool,dst=/workspace/tools/$tool,readonly")
    done
    captured_verify+=(--mount "type=bind,src=$capture/context/requirements/checker-dependencies.json,dst=/input/dependencies.json,readonly"
        --mount "type=bind,src=$capture/dependencies,dst=/dependencies,readonly"
        "$base" python3 -B -m tools.checker check --declaration /input/dependencies.json
        --directory /dependencies --base "$base")
    "${captured_verify[@]}" >/dev/null
    input_id="$(cd -- "$capture/context"; { sha256sum -- "${inputs[@]}"; stat -c '%a %n' -- "${inputs[@]}"; } | sha256sum | cut -d ' ' -f1)"
else
    input_id=PRINT
fi
build=("${engine[@]}" build --builder default --quiet --network none --pull=false
    --label "io.nmrnet-repro.checker-inputs=$input_id"
    --build-context "dependencies=$capture/dependencies"
    --file "$capture/context/containers/checker/Dockerfile" "$capture/context")
if "$print_command"; then run "${build[@]}"; exit; fi
image_id="$("${build[@]}")"
if [[ ! "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    printf '%s\n' 'Cannot record checker image: build did not return one full immutable image ID.' >&2
    exit 2
fi
observed="$("${engine[@]}" image inspect --format '{{.Id}} {{index .Config.Labels "io.nmrnet-repro.checker-inputs"}}' "$image_id")"
if [[ "$observed" != "$image_id $input_id" ]]; then
    printf '%s\n' 'Cannot record checker image: observed identity or captured-input label differs.' >&2
    exit 2
fi
record="$repository_root/.work/checker/image-id"
if [[ -L "$record" || ( -e "$record" && ! -f "$record" ) ]]; then
    printf '%s\n' 'Cannot record checker image: existing image record is not a regular file.' >&2
    exit 2
fi
temporary_record="$(mktemp "$repository_root/.work/checker/.image-id.XXXXXXXX")"
printf '%s\n' "$image_id" > "$temporary_record"
mv -T -- "$temporary_record" "$record"
temporary_record=
printf 'Checker image recorded: %s\n' "$image_id"
