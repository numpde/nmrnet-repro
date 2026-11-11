#!/usr/bin/env bash
# The acquisition worker and shared publisher lock the pool while writing;
# checks and consumers hold shared locks while selecting its artifacts.
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
    printf '%s\n' 'Usage: bash tools/wheelhouse.sh [--print] {download|check} [inference|preparation|provider-core|provider-support|shared-tool]' >&2
    exit 2
fi
if [[ $# == 2 ]]; then profile=$2; else profile=inference; fi
case "$profile" in
    inference)
        inputs=(inference.lock preparation.lock unicore-source.json provider-core-native-install.lock provider-core-install.lock wheelhouse-tool-assets.in)
        build_target=packages/image
        worker_arguments=(--lock /input/inference.lock --lock /input/preparation.lock --lock /input/provider-core-native-install.lock --lock /input/provider-core-install.lock --source /input/unicore-source.json --stream-assets /input/wheelhouse-tool-assets.in --pool /wheelhouse)
        ;;
    preparation)
        inputs=(preparation.lock provider-core-native-install.lock wheelhouse-tool-assets.in)
        build_target=geometry/image/build
        worker_arguments=(--lock /input/preparation.lock --lock /input/provider-core-native-install.lock --stream-assets /input/wheelhouse-tool-assets.in --pool /wheelhouse)
        ;;
    provider-core)
        inputs=(provider-core-install.lock wheelhouse-tool-assets.in)
        build_target=provider/image/build
        worker_arguments=(--lock /input/provider-core-install.lock --stream-assets /input/wheelhouse-tool-assets.in --pool /wheelhouse)
        ;;
    shared-tool)
        inputs=(wheelhouse-tool-assets.in)
        worker_arguments=(--profile shared-tool --assets /input/wheelhouse-tool-assets.in --pool /wheelhouse)
        ;;
    provider-support)
        inputs=(provider-core-assets.in)
        worker_arguments=(--support --assets /input/provider-core-assets.in --pool /wheelhouse)
        ;;
    *) printf '%s\n' 'Cannot select wheelhouse profile: choose inference, preparation, provider-core, provider-support or shared-tool.' >&2; exit 2 ;;
esac
source "$repository_root/tools/wheelhouse_path.sh"
select_wheelhouse
source "$repository_root/tools/operator_guidance.sh"
shared_acquisition=true
if [[ $profile == shared-tool ]]; then
    shared_acquisition=false
elif [[ $1 == check ]]; then
    worker_arguments+=(--check)
elif [[ $profile != provider-support ]]; then
    inputs+=(provider-core-assets.in wheelhouse-acquirer-assets.in)
    worker_arguments+=(--assets /input/provider-core-assets.in --tool-assets /input/wheelhouse-acquirer-assets.in)
    if [[ $profile == inference ]]; then
        inputs+=(inference.in)
        worker_arguments+=(--declaration /input/inference.in)
    fi
fi
parent_directory=$wheelhouse_directory
container_parent=/wheelhouse
# The pool itself is the only persistent artifact location.
if [[ "$1" == download ]]; then
    if [[ $repository_root == "$parent_directory" || $repository_root == "$parent_directory/"* ]]; then
        printf '%s\n' 'Wheel acquisition cannot mount the repository or its ancestors writable; select a cache directory.' >&2
        exit 2
    fi
fi
if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
    printf '%s\n' 'Wheelhouse lanes require a nonzero host UID and GID.' >&2
    exit 2
fi
next_actions=
if [[ $profile != shared-tool && $profile != provider-support ]]; then
    next_actions=$(next_make_command 'Next, build the image from these verified dependencies:' "$build_target" "WHEELHOUSE=$wheelhouse_directory")
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
    source "$repository_root/tools/acquisition_interface.sh"
    source "$repository_root/tools/inference_native.sh"
    if "$print_command" || [[ -n ${NMRNET_NETWORK_INTERFACE_INPUT-} || ${NMRNET_NETWORK_INTERFACE_INPUT_ORIGIN-} == 'command line' ]]; then
        select_acquisition_interface
    fi
    if ! "$print_command"; then
        mkdir -p -- "$parent_directory"
        pool_owner=$(stat -c '%u:%g' -- "$parent_directory")
        if [[ $pool_owner != "$caller_uid:$caller_gid" ]]; then
            printf 'Wheel acquisition requires the shared pool owner to match this launcher (%s); observed %s.\n' \
                "$caller_uid:$caller_gid" "$pool_owner" >&2
            exit 2
        fi
    else
        pool_owner=$caller_uid:$caller_gid
    fi
        readiness_arguments=("${worker_arguments[@]}" --probe)
        if [[ $profile == shared-tool ]]; then
            readiness_arguments=(--tool-only --assets /input/wheelhouse-tool-assets.in --pool /wheelhouse --probe)
        fi
        offline_grants=(--read-only --env NMRNET_IN_CONTAINER=1
            --cap-drop ALL --security-opt no-new-privileges:true --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64
            --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --workdir /workspace
            --mount "type=bind,src=$repository_root/tools/wheelhouse.py,dst=/workspace/tools/wheelhouse.py,readonly"
            --mount "type=bind,src=$repository_root/tools/wheelhouse_acquire.py,dst=/workspace/tools/wheelhouse_acquire.py,readonly"
            --mount "type=bind,src=$repository_root/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
            --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly")
        for input in "${inputs[@]}"; do
            offline_grants+=(--mount "type=bind,src=$repository_root/requirements/$input,dst=/input/$input,readonly")
        done
        offline_grants+=(--mount "type=bind,src=$parent_directory,dst=/wheelhouse,readonly")
        offline_app=(python3 -B -m tools.wheelhouse_acquire "${readiness_arguments[@]}")
    command=(bash "$repository_root/tools/docker.sh")
    if "$print_command"; then command+=(--print); fi
    if "$print_command"; then command+=(--success-note "$next_actions"); else command+=(--success-note ''); fi
    command+=(--pool-owner "$pool_owner" run)
    output_mount="type=bind,src=$parent_directory,dst=$container_parent"
    memory=2g
    scratch=1g
else
    if [[ ! -d "$parent_directory" ]]; then
        printf 'Cannot check wheelhouse: %s is missing.\n' "$parent_directory" >&2
        exit 2
    fi
    if ! "$print_command"; then lock_wheelhouse shared; fi
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
    if "$print_command"; then docker_client_prepare print || { printf '%s' "$docker_client_diagnostic" >&2; exit 2; }
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
worker_grant_start=${#command[@]}
command+=(--pull never --read-only --env NMRNET_IN_CONTAINER=1 --user "$caller_uid:$caller_gid"
    --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 2 --memory "$memory" --memory-swap "$memory" --pids-limit 64
    --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=$scratch"
    --workdir /workspace
    --mount "type=bind,src=$repository_root/tools/wheelhouse.py,dst=/workspace/tools/wheelhouse.py,readonly"
    --mount "type=bind,src=$repository_root/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
    --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly")
if "$shared_acquisition"; then
    command+=(--mount "type=bind,src=$repository_root/tools/wheelhouse_acquire.py,dst=/workspace/tools/wheelhouse_acquire.py,readonly")
fi
for input in "${inputs[@]}"; do
    command+=(--mount "type=bind,src=$repository_root/requirements/$input,dst=/input/$input,readonly")
done
if [[ "$1" == download ]]; then
    online_grants=("${command[@]:worker_grant_start}")
    # The shared owner supplies the selected engine's mapped worker identity.
    for index in "${!online_grants[@]}"; do
        if [[ ${online_grants[index]} == --user ]]; then
            unset 'online_grants[index]' 'online_grants[index+1]'
            break
        fi
    done
    online_grants=("${online_grants[@]}" --mount "$output_mount")
    if "$shared_acquisition"; then
        online_app=(python3 -B -m tools.wheelhouse_acquire "${worker_arguments[@]}")
    else
        online_app=(python3 -B -m tools.wheelhouse "$1" "${worker_arguments[@]}")
    fi
fi
if "$shared_acquisition"; then
    command+=(--mount "$output_mount" "$image" python3 -B -m tools.wheelhouse_acquire "${worker_arguments[@]}")
else
    command+=(--mount "$output_mount" "$image" python3 -B -m tools.wheelhouse "$1" "${worker_arguments[@]}")
fi
if [[ "$1" == download ]]; then
    source "$repository_root/tools/docker_client.sh"
    docker_client_prepare print || { printf '%s' "$docker_client_diagnostic" >&2; exit 2; }
    if "$print_command"; then
        printf 'Docker context: %s (%s)\n' "$NMRNET_DOCKER_CONTEXT_INPUT" "$NMRNET_DOCKER_ENDPOINT_INPUT"
        printf 'If needed, acquisition route: %s\n' "$interface"
        printf 'Offline probe: '; printf '%q ' "$image" "${offline_grants[@]}" -- "${offline_app[@]}"; printf '\n'
        printf 'If missing, acquire: '; printf '%q ' "$image" "${online_grants[@]}" -- "${online_app[@]}"; printf '\n'
        printf '%s\n' 'After acquisition, repeat the offline probe.'
        exit
    fi
    python3 -B "$repository_root/host/acquisition_bootstrap.py"
    source "$repository_root/host/bootstrap/acquisition/acquisition_tool/owner.sh"
    source "$repository_root/tools/acquisition_wheelhouse.sh"
    wheelhouse_warm=false
    if acquisition_scope --endpoint "$NMRNET_DOCKER_ENDPOINT_INPUT" --bootstrap-image "$image" -- nmrnet_acquire_wheelhouse; then :; else exit "$?"; fi
    if "$wheelhouse_warm"; then printf '%s\n' 'All wheelhouse inputs are already available; no download is needed.'; fi
    if [[ -n $next_actions ]]; then report_next_actions "$next_actions"; fi
elif "$print_command"; then
    printf '%q ' "${command[@]}"; printf '\n'
else
    inference_native 'wheelhouse verification' untimed stdio "${command[@]}"
fi
