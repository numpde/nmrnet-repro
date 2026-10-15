#!/usr/bin/env bash
# Acquisition writes only staging; verification reads the completed wheelhouse offline.
# SECS package lanes separate networked wheel acquisition from offline image builds:
# https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/make/packages.mk
# This lane keeps that boundary with the NMRNet lock and pinned Uni-Core source.
set -euo pipefail

readonly image='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly caller_uid="$(id -u)"
readonly caller_gid="$(id -g)"
print_command=false
if [[ $# -gt 0 && "$1" == --print ]]; then print_command=true; shift; fi
if [[ $# != 1 || ! "$1" =~ ^(download|check)$ ]]; then
    printf '%s\n' 'Usage: bash tools/wheelhouse.sh [--print] {download|check}' >&2
    exit 2
fi
if [[ "$1" == download ]]; then
    bash "$repository_root/tools/docker.sh" check-interface
    if ! "$print_command"; then
        bash "$repository_root/tools/docker.sh" require-images "$image"
    fi
fi
if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
    printf '%s\n' 'Wheelhouse lanes require a nonzero host UID and GID.' >&2
    exit 2
fi
if [[ "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
    printf '%s\n' 'Repository path cannot contain commas or line breaks in Docker bind mounts.' >&2
    exit 2
fi
for directory in "$repository_root/.work" "$repository_root/.work/wheelhouse"; do
    if [[ -L "$directory" || ( -e "$directory" && ! -d "$directory" ) ]]; then
        printf 'Wheelhouse path must be a directory without a symlink: %s\n' "$directory" >&2
        exit 2
    fi
done
for input in inference.lock unicore-source.json; do
    if [[ ! -f "$repository_root/requirements/$input" || -L "$repository_root/requirements/$input" ]]; then
        printf 'Wheelhouse input must be a regular file without a symlink: %s\n' "$input" >&2
        exit 2
    fi
done
if [[ "$1" == download ]]; then
    if ! "$print_command"; then mkdir -p -- "$repository_root/.work/wheelhouse"; fi
    command=(bash "$repository_root/tools/docker.sh")
    if "$print_command"; then command+=(--print); fi
    command+=(run)
    output_mount="type=bind,src=$repository_root/.work/wheelhouse,dst=/wheelhouse"
    memory=2g
    scratch=1g
else
    if [[ ! -d "$repository_root/.work/wheelhouse" ]]; then
        printf '%s\n' 'Cannot check wheelhouse: .work/wheelhouse is missing.' >&2
        exit 2
    fi
    source "$repository_root/tools/docker_client.sh"
    source "$repository_root/tools/inference_native.sh"
    docker_client_directory=
    inference_diagnostic=
    inference_cancellation_status=0
    caller_cancellation=0
    # Only offline verification owns this private client. Acquisition remains
    # with docker.sh; no native child exists before preparation here.
    inference_retirement_confirmed=true
    cleanup() {
        local status=$? cleanup_status=0 cancelled=0
        local native_cancellation=$inference_cancellation_status
        trap - EXIT
        # Quiet removal precedes reporting, as in inference_image.sh. Cancellation
        # during removal finishes it without starting another native owner.
        trap 'cancelled=129' HUP
        trap 'cancelled=130' INT
        trap 'cancelled=143' TERM
        if "$inference_retirement_confirmed"; then
            if docker_client_cleanup 2>/dev/null; then :; else
                cleanup_status=$?
                if [[ $status == 0 ]]; then status=$cleanup_status; fi
            fi
        fi
        trap 'caller_cancellation=129; exit 129' HUP
        trap 'caller_cancellation=130; exit 130' INT
        trap 'caller_cancellation=143; exit 143' TERM
        if [[ $cancelled != 0 ]]; then exit "$cancelled"; fi
        if [[ $caller_cancellation != 0 ]]; then exit "$caller_cancellation"; fi
        if [[ $native_cancellation != 0 ]]; then exit "$native_cancellation"; fi
        if ! "$inference_retirement_confirmed"; then
            printf '%s' "$inference_diagnostic" >&2
            if [[ -n "$docker_client_directory" ]]; then
                printf 'Cannot confirm wheelhouse native client retirement; private Docker client state retained at %q.\n' "$docker_client_directory" >&2
            fi
        elif [[ -n "$inference_diagnostic" || -n "$docker_client_directory" ]]; then
            # Expand the returned text before this terminal reporter resets it;
            # no retry/direct fallback follows even if bootstrap fails.
            if inference_native 'wheelhouse client cleanup reporting' untimed stdio bash -eu -c '
                printf "%s" "$1" >&2
                if [[ -n "$2" ]]; then
                    printf "Cannot confirm wheelhouse Docker client configuration removal; attempted path %q (status %s).\n" "$2" "$3" >&2
                fi
            ' bash "$inference_diagnostic" "$docker_client_directory" "$cleanup_status"; then :; else
                local report_status=$?
                if [[ $inference_cancellation_status != 0 ]]; then
                    status=$inference_cancellation_status
                elif [[ $status == 0 ]]; then status=$report_status; fi
            fi
        fi
        exit "$status"
    }
    trap cleanup EXIT
    trap 'caller_cancellation=129; exit 129' HUP
    trap 'caller_cancellation=130; exit 130' INT
    trap 'caller_cancellation=143; exit 143' TERM
    if "$print_command"; then docker_client_prepare print
    elif docker_client_prepare run; then :
    else
        preparation_status=$?
        inference_diagnostic+=$docker_client_diagnostic
        exit "$preparation_status"
    fi
    command=("${engine[@]}" run --rm --network none)
    output_mount="type=bind,src=$repository_root/.work/wheelhouse,dst=/wheelhouse,readonly"
    memory=512m
    scratch=64m
fi
command+=(--pull never --read-only --env NMRNET_IN_CONTAINER=1 --user "$caller_uid:$caller_gid"
    --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 2 --memory "$memory" --memory-swap "$memory" --pids-limit 64
    --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=$scratch"
    --workdir /workspace
    --mount "type=bind,src=$repository_root/tools/wheelhouse.py,dst=/workspace/tools/wheelhouse.py,readonly"
    --mount "type=bind,src=$repository_root/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
    --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
    --mount "type=bind,src=$repository_root/requirements/inference.lock,dst=/input/inference.lock,readonly"
    --mount "type=bind,src=$repository_root/requirements/unicore-source.json,dst=/input/unicore-source.json,readonly"
    --mount "$output_mount"
    "$image" python3 -B -m tools.wheelhouse "$1"
    --lock /input/inference.lock --source /input/unicore-source.json --output /wheelhouse/cpu)
if [[ "$1" == download ]]; then
    exec "${command[@]}"
elif "$print_command"; then
    printf '%q ' "${command[@]}"; printf '\n'
else
    inference_native 'wheelhouse verification' untimed stdio "${command[@]}"
fi
