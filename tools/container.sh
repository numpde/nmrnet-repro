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
    printf '%s\n' 'Usage: bash tools/container.sh [--print] {pull|test|test/repository|probe|download|check|inspect|extract}' >&2
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

require_source_directory() {
    if [[ ! -d "$repository_root/$1" || -L "$repository_root/$1" ]]; then
        printf 'Cannot run contained checks: %s must be a directory without a symlink.\n' "$1" >&2
        exit 2
    fi
}

image_input_directory=
check_input_directory=

checker_image() {
    local record="$repository_root/.work/checker/image-id" inspect_status
    if "$print_command"; then
        checker_image_id=CHECKER_IMAGE_ID_FROM_RECORD
        return
    fi
    for directory in "$repository_root/.work" "$repository_root/.work/checker"; do
        if [[ ! -d "$directory" || -L "$directory" ]]; then
            printf '%s\n' 'Cannot run repository checks: checker record directories must exist without symlinks.' >&2
            exit 2
        fi
    done
    if [[ ! -f "$record" || -L "$record" || "$(stat -c '%s' "$record")" != 72 ]] \
        || ! LC_ALL=C grep -aExq 'sha256:[0-9a-f]{64}' "$record"; then
        printf '%s\n' 'Cannot run repository checks: image-id must contain exactly one full immutable image ID and a newline; prepare checks/image/build explicitly.' >&2
        exit 2
    fi
    IFS= read -r checker_image_id < "$record"
    # Compare original bytes before using a shell value; Bash discards NUL bytes.
    if ! cmp -s -- "$record" <(printf '%s\n' "$checker_image_id"); then
        printf '%s\n' 'Cannot run repository checks: checker image record is not byte-exact.' >&2
        exit 2
    fi
    image_input_directory="$(mktemp -d "$repository_root/.work/checker/.inspect.XXXXXXXX")"
    trap cleanup_check_inputs EXIT
    (umask 077; : > "$image_input_directory/observed-id"; : > "$image_input_directory/diagnostic")
    # Native receipts stay as bytes. Capture files are private (0700 directory,
    # 0600 files), with a 4 KiB per-file limit and a bounded inspect deadline.
    if (umask 077; ulimit -f 4
        timeout --signal=TERM --kill-after=2s 10s "${engine[@]}" image inspect --format '{{.Id}}' "$checker_image_id"
        ) > "$image_input_directory/observed-id" 2> "$image_input_directory/diagnostic"; then :; else
        inspect_status=$?
        printf 'Cannot run repository checks: checker image inspection failed (status %s); prepare checks/image/build explicitly if the recorded image is unavailable.\n' "$inspect_status" >&2
        exit "$inspect_status"
    fi
    if ! cmp -s -- "$image_input_directory/observed-id" <(printf '%s\n' "$checker_image_id"); then
        printf '%s\n' 'Cannot run repository checks: observed checker image identity differs from its byte-exact immutable record.' >&2
        exit 2
    fi
}

cleanup_check_inputs() {
    local status=$? removal_status
    trap - EXIT
    if [[ -n "$image_input_directory" ]]; then
        if rm -rf -- "$image_input_directory"; then :; else
            removal_status=$?
            printf 'Cannot remove owned checker image receipt: %s\n' "$image_input_directory" >&2
            if [[ "$status" == 0 ]]; then status=$removal_status; fi
        fi
    fi
    if [[ -z "$check_input_directory" ]]; then exit "$status"; fi
    if rm -rf -- "$check_input_directory/git"; then :; else
        removal_status=$?
        printf 'Cannot remove owned checker Git scaffold: %s\n' "$check_input_directory/git" >&2
        if [[ "$status" == 0 ]]; then status=$removal_status; fi
    fi
    if rm -f -- "$check_input_directory/root-entries" && rmdir -- "$check_input_directory"; then :; else
        removal_status=$?
        printf 'Cannot remove owned checker source inventory: %s\n' "$check_input_directory" >&2
        if [[ "$status" == 0 ]]; then status=$removal_status; fi
    fi
    exit "$status"
}

case "$1" in
    pull)
        printf '%s\n' 'Base images must be preloaded: Docker daemon pulls cannot be bound by this launcher to WLAN_INTERFACE.' >&2
        printf 'Provision this pinned image through an independently interface-bound process: %s\n' "$image" >&2
        exit 2
        ;;
    test|test/repository|probe|download|check|inspect|extract)
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
            # Make owns public input origin and hands over only its literal
            # value alias. Ambient ARCHIVE must not select an archive action.
            if [[ ! -v NMRNET_ARCHIVE_INPUT ]] || [[ ! "$NMRNET_ARCHIVE_INPUT" =~ ^[A-Za-z0-9_.-]+\.zip$ ]]; then
                printf '%s\n' 'Select an archive on the make command line, for example make weights/check ARCHIVE=weight.zip.' >&2
                exit 2
            fi
            archive="$NMRNET_ARCHIVE_INPUT"
            if [[ "$1" == download ]]; then
                network=host
                prepare_directory "$repository_root/.work"
                prepare_directory "$repository_root/.work/downloads"
            elif [[ -L "$repository_root/.work" || -L "$repository_root/.work/downloads" || ! -d "$repository_root/.work/downloads" ]]; then
                printf '%s\n' 'Cannot read archives: .work/downloads must be an existing directory without symlinks.' >&2
                exit 2
            fi
            if [[ "$1" == extract ]]; then
                selection="$repository_root/artifacts/${archive%.zip}.selection.json"
                if [[ ! -f "$selection" || -L "$selection" ]]; then
                    printf 'Cannot extract %s: expected a regular selection declaration at %s.\n' "$archive" "$selection" >&2
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
        if [[ "$1" == test || "$1" == test/repository ]]; then
            # Native cancellation probes retire whole process groups. An init
            # process reaps orphaned descendants; Python as PID 1 would retain
            # zombies until the fixed 64-process budget was exhausted.
            command+=(--init)
        fi
        case "$1" in
            test)
                require_source_directory tools
                require_source_directory tests
                command+=(--mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/tests,dst=/workspace/tests,readonly"
                    "$image" python3 -B -m unittest discover -s tests -v)
                ;;
            test/repository)
                # Checkers read public source inputs, never workspace state or credentials.
                # A root-entry inventory makes omitted new source paths fail visibly.
                require_source_directory tools
                require_source_directory repository_checks
                if [[ ! -f "$repository_root/Makefile" || -L "$repository_root/Makefile" ]]; then
                    printf '%s\n' 'Cannot run repository checks: Makefile must be a regular file without a symlink.' >&2
                    exit 2
                fi
                checker_image
                check_input_directory="$repository_root/.work/repository-check-inputs.PRINT"
                if ! "$print_command"; then
                    prepare_directory "$repository_root/.work"
                    check_input_directory="$(mktemp -d "$repository_root/.work/repository-check-inputs.XXXXXXXX")"
                    trap cleanup_check_inputs EXIT
                    find "$repository_root" -mindepth 1 -maxdepth 1 -printf '%y\t%f\0' > "$check_input_directory/root-entries"
                    bash "$repository_root/tools/check_git_inputs.sh" "$repository_root" "$check_input_directory/git"
                fi
                command+=(--mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/repository_checks,dst=/workspace/repository_checks,readonly"
                    --mount "type=bind,src=$repository_root/Makefile,dst=/workspace/Makefile,readonly"
                    --mount "type=bind,src=$check_input_directory/root-entries,dst=/repository-source-inventory/root-entries,readonly"
                    --mount "type=bind,src=$check_input_directory/git,dst=/workspace/.git,readonly"
                    --mount "type=bind,src=$repository_root/.git/objects,dst=/workspace/.git/objects,readonly"
                    --mount "type=bind,src=$repository_root/.git/modules/nmrnet/objects,dst=/workspace/.git/modules/nmrnet/objects,readonly"
                    --env "NMRNET_EXPECTED_UID=$caller_uid" --env "NMRNET_EXPECTED_GID=$caller_gid")
                for source_input in tests containers requirements artifacts notes NMRNetplusplus README.md .gitignore .gitmodules .github GNUmakefile makefile; do
                    if [[ -e "$repository_root/$source_input" || -L "$repository_root/$source_input" ]]; then
                        if [[ -L "$repository_root/$source_input" || ( ! -f "$repository_root/$source_input" && ! -d "$repository_root/$source_input" ) ]]; then
                            printf 'Cannot check repository source: public input must be a regular file or directory without a symlink: %s\n' "$source_input" >&2
                            exit 2
                        fi
                        command+=(--mount "type=bind,src=$repository_root/$source_input,dst=/workspace/$source_input,readonly")
                    fi
                done
                # Include the future production package without granting the full checkout.
                if [[ -e "$repository_root/nmrnet_inference" || -L "$repository_root/nmrnet_inference" ]]; then
                    if [[ -L "$repository_root/nmrnet_inference" || ! -d "$repository_root/nmrnet_inference" ]]; then
                        printf '%s\n' 'Cannot check repository conventions: nmrnet_inference must be a directory without a symlink.' >&2
                        exit 2
                    fi
                    command+=(--mount "type=bind,src=$repository_root/nmrnet_inference,dst=/workspace/nmrnet_inference,readonly")
                fi
                # Git ignores safe.directory in local config. A contained global
                # config admits only these root-owned mount destinations; HOME
                # remains available when source verification scrubs GIT_* vars.
                command+=(--env HOME=/tmp/checker-home "$checker_image_id" bash -eu -c
                    'mkdir -m 700 /tmp/checker-home; printf "%s\n" "[safe]" "directory = /workspace" "directory = /workspace/NMRNetplusplus" > /tmp/checker-home/.gitconfig; exec python3 -B -m unittest discover -s repository_checks -v')
                ;;
            download|check|inspect|extract)
                archive_mount="type=bind,src=$repository_root/.work/downloads,dst=/downloads"
                if [[ "$1" != download ]]; then archive_mount+=,readonly; fi
                # Module launch keeps the artifact owner and shared guard in one namespace.
                command+=(--mount "type=bind,src=$repository_root/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
                    --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
                    --mount "type=bind,src=$repository_root/artifacts/release.json,dst=/inputs/release.json,readonly"
                    --mount "$archive_mount")
                if [[ "$1" == extract ]]; then
                    command+=(--mount "type=bind,src=$selection,dst=/inputs/selection.json,readonly"
                        --mount "type=bind,src=$repository_root/.work/prepared,dst=/prepared")
                fi
                command+=("$image" python3 -B -m tools.artifacts "$1"
                    --declaration /inputs/release.json --directory /downloads --name "$archive")
                if [[ "$1" == extract ]]; then
                    command+=(--selection /inputs/selection.json --output "/prepared/${archive%.zip}")
                fi
                ;;
            probe)
                prepare_directory "$repository_root/.work"
                probe_directory="$repository_root/.work/probe.PRINT"
                if ! "$print_command"; then
                    probe_directory="$(mktemp -d "$repository_root/.work/probe.XXXXXXXX")"
                    trap 'rm -f -- "$probe_directory/ownership"; rmdir -- "$probe_directory"' EXIT
                fi
                command+=(--mount "type=bind,src=$repository_root/tools/probe.py,dst=/workspace/tools/probe.py,readonly"
                    --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
                    --mount "type=bind,src=$probe_directory,dst=/output"
                    "$image" python3 -B -m tools.probe "$caller_uid" "$caller_gid")
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
    if [[ "$1" == test/repository ]]; then
        # Keep the shell alive so its EXIT trap removes the metadata input.
        "${command[@]}"
    elif [[ "$1" == probe ]]; then
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
