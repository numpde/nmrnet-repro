#!/usr/bin/env bash
# Acquisition stages and publishes individual files; checks select locally offline.
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
if [[ $# -lt 1 || $# -gt 2 || ! "$1" =~ ^(download|check)$ ]]; then
    printf '%s\n' 'Usage: bash tools/wheelhouse.sh [--print] {download|check} [inference|preparation|provider-core]' >&2
    exit 2
fi
if [[ $# == 2 ]]; then profile=$2; else profile=inference; fi
case "$profile" in
    inference)
        inputs=(inference.lock unicore-source.json)
        build_target=packages/image
        worker_arguments=(--profile inference --lock /input/inference.lock --source /input/unicore-source.json --pool /wheelhouse)
        ;;
    preparation)
        inputs=(preparation.lock)
        build_target=geometry/image/build
        worker_arguments=(--profile preparation --lock /input/preparation.lock --pool /wheelhouse)
        ;;
    provider-core)
        inputs=(provider-core.lock provider-core-build.lock)
        build_target=provider/image/build
        worker_arguments=(--profile provider-core --lock /input/provider-core.lock --lock /input/provider-core-build.lock --pool /wheelhouse)
        ;;
    *) printf '%s\n' 'Cannot select wheelhouse profile: choose inference, preparation or provider-core.' >&2; exit 2 ;;
esac
source "$repository_root/tools/wheelhouse_path.sh"
select_wheelhouse
source "$repository_root/tools/operator_guidance.sh"
next_actions=$(next_make_command 'Next, build the image from these verified dependencies:' "$build_target" "WHEELHOUSE=$wheelhouse_directory")
parent_directory=$wheelhouse_directory
container_parent=/wheelhouse
# Private staging and per-file atomic publication share this one mount. The
# pool is never replaced, cleared or assigned a whole-directory receipt.
if [[ "$1" == download ]]; then
    if [[ $repository_root == "$parent_directory" || $repository_root == "$parent_directory/"* ]]; then
        printf '%s\n' 'Wheel acquisition cannot mount the repository or its ancestors writable; select a cache directory.' >&2
        exit 2
    fi
    source "$repository_root/tools/acquisition_interface.sh"
    select_acquisition_interface
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
for input in "${inputs[@]}"; do
    if [[ ! -f "$repository_root/requirements/$input" || -L "$repository_root/requirements/$input" ]]; then
        printf 'Wheelhouse input must be a regular file without a symlink: %s\n' "$input" >&2
        exit 2
    fi
done
if [[ "$1" == download ]]; then
    if ! "$print_command"; then mkdir -p -- "$parent_directory"; fi
    command=(bash "$repository_root/tools/docker.sh")
    if "$print_command"; then command+=(--print); fi
    command+=(--success-note "$next_actions" run)
    output_mount="type=bind,src=$parent_directory,dst=$container_parent"
    memory=2g
    scratch=1g
else
    if [[ ! -d "$parent_directory" ]]; then
        printf 'Cannot check wheelhouse: %s is missing.\n' "$parent_directory" >&2
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
        if [[ $status == 0 && $print_command == false ]]; then
            if report_next_actions "$next_actions"; then :; else status=$?; fi
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
    output_mount="type=bind,src=$parent_directory,dst=$container_parent,readonly"
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
    --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly")
for input in "${inputs[@]}"; do
    command+=(--mount "type=bind,src=$repository_root/requirements/$input,dst=/input/$input,readonly")
done
command+=(--mount "$output_mount" "$image" python3 -B -m tools.wheelhouse "$1" "${worker_arguments[@]}")
if [[ "$1" == download ]]; then
    exec "${command[@]}"
elif "$print_command"; then
    printf '%q ' "${command[@]}"; printf '\n'
else
    inference_native 'wheelhouse verification' untimed stdio "${command[@]}"
fi
