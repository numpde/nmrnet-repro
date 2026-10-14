#!/usr/bin/env bash
# Host orchestration only; repository Python runs in explicit contained lanes.
set -euo pipefail

readonly image='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly caller_uid="$(id -u)"
readonly caller_gid="$(id -g)"

print_command=false
if [[ "${1:-}" == --print ]]; then
    print_command=true
    shift
fi
if [[ $# -ne 1 ]]; then
    printf '%s\n' 'Usage: bash tools/container.sh [--print] {pull|test|probe|download|check|inspect|extract}' >&2
    exit 2
fi

# Bind this launcher to the local Docker engine, independent of ambient contexts.
engine=(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH
    docker --host unix:///var/run/docker.sock)
command=("${engine[@]}")

prepare_directory() {
    local directory="$1"
    if [[ -L "$directory" || ( -e "$directory" && ! -d "$directory" ) ]]; then
        printf 'Cannot use artifact directory; expected a directory without a symlink: %s\n' "$directory" >&2
        exit 2
    fi
    if ! "$print_command"; then
        mkdir -p -- "$directory"
    fi
}

case "$1" in
    pull)
        printf '%s\n' 'Base images must be preloaded: Docker daemon pulls cannot be bound by this launcher to WLAN_INTERFACE.' >&2
        printf 'Provision this pinned image through an independently interface-bound process: %s\n' "$image" >&2
        exit 2
        ;;
    test|probe|download|check|inspect|extract)
        if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
            printf '%s\n' 'Contained lanes require a nonzero host UID and GID.' >&2
            exit 2
        fi
        if [[ "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
            printf '%s\n' 'Repository path cannot contain commas or line breaks in Docker bind mounts.' >&2
            exit 2
        fi
        network=none
        if [[ "$1" == download ]]; then
            bash "$repository_root/tools/docker.sh" check-interface
        fi
        if [[ "$1" == download || "$1" == check || "$1" == inspect || "$1" == extract ]]; then
            if [[ ! "${ARCHIVE:-}" =~ ^[A-Za-z0-9_.-]+\.zip$ ]]; then
                printf '%s\n' 'Set ARCHIVE to one declared archive name, for example ARCHIVE=weight.zip.' >&2
                exit 2
            fi
            if [[ "$1" == download ]]; then
                network=host
                prepare_directory "$repository_root/.work"
                prepare_directory "$repository_root/.work/downloads"
            elif [[ -L "$repository_root/.work" || -L "$repository_root/.work/downloads" || ! -d "$repository_root/.work/downloads" ]]; then
                printf '%s\n' 'Cannot read archives: .work/downloads must be an existing directory without symlinks.' >&2
                exit 2
            fi
            if [[ "$1" == extract ]]; then
                selection="$repository_root/artifacts/${ARCHIVE%.zip}.selection.json"
                if [[ ! -f "$selection" || -L "$selection" ]]; then
                    printf 'Cannot extract %s: expected a regular selection declaration at %s.\n' "$ARCHIVE" "$selection" >&2
                    exit 2
                fi
                prepare_directory "$repository_root/.work/prepared"
            fi
        fi
        if [[ "$network" == host ]]; then
            command=(bash "$repository_root/tools/docker.sh")
            if "$print_command"; then command+=(--print); fi
            command+=(run)
        else
            command+=(run --rm --network none)
        fi
        command+=(--pull never --read-only --env NMRNET_IN_CONTAINER=1
            --user "$caller_uid:$caller_gid" --cap-drop ALL
            --security-opt no-new-privileges:true
            --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64
            --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m
            --workdir /workspace)
        case "$1" in
            test)
                command+=(--mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/tests,dst=/workspace/tests,readonly"
                    "$image" python3 -B -m unittest discover -s tests -v)
                ;;
            download|check|inspect|extract)
                archive_mount="type=bind,src=$repository_root/.work/downloads,dst=/downloads"
                if [[ "$1" != download ]]; then archive_mount+=,readonly; fi
                command+=(--mount "type=bind,src=$repository_root/tools/artifacts.py,dst=/workspace/artifacts.py,readonly"
                    --mount "type=bind,src=$repository_root/artifacts/release.json,dst=/inputs/release.json,readonly"
                    --mount "$archive_mount")
                if [[ "$1" == extract ]]; then
                    command+=(--mount "type=bind,src=$selection,dst=/inputs/selection.json,readonly"
                        --mount "type=bind,src=$repository_root/.work/prepared,dst=/prepared")
                fi
                command+=("$image" python3 -B /workspace/artifacts.py "$1"
                    --declaration /inputs/release.json --directory /downloads --name "$ARCHIVE")
                if [[ "$1" == extract ]]; then
                    command+=(--selection /inputs/selection.json --output "/prepared/${ARCHIVE%.zip}")
                fi
                ;;
            probe)
                prepare_directory "$repository_root/.work"
                probe_directory="$repository_root/.work/probe.PRINT"
                if ! "$print_command"; then
                    probe_directory="$(mktemp -d "$repository_root/.work/probe.XXXXXXXX")"
                    trap 'rm -f -- "$probe_directory/ownership"; rmdir -- "$probe_directory"' EXIT
                fi
                command+=(--mount "type=bind,src=$repository_root/tools/probe.py,dst=/workspace/probe.py,readonly"
                    --mount "type=bind,src=$probe_directory,dst=/output"
                    "$image" python3 -B /workspace/probe.py "$caller_uid" "$caller_gid")
                ;;
        esac
        ;;
    *)
        printf 'Unknown container lane: %s\n' "$1" >&2
        exit 2
        ;;
esac

if "$print_command"; then
    if [[ "$1" == download ]]; then
        "${command[@]}"
    else
        printf '%q ' "${command[@]}"
        printf '\n'
    fi
else
    if [[ "$1" == probe ]]; then
        "${command[@]}"
        observed_owner="$(stat -c '%u:%g' "$probe_directory/ownership")"
        if [[ "$observed_owner" != "$caller_uid:$caller_gid" ]]; then
            printf 'Probe output ownership is %s; expected %s:%s.\n' "$observed_owner" "$caller_uid" "$caller_gid" >&2
            exit 1
        fi
        printf 'Probe output ownership: %s\n' "$observed_owner"
    else
        exec "${command[@]}"
    fi
fi
