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
apply_capture=
# Presentation only: EXIT publishes this caller's rejection after quiet cleanup.
inference_diagnostic=
inference_cancellation_status=0
caller_cancellation=0
# Only this launcher's build/apply clients use this config. Stage acquisition
# remains owned by docker.sh; no native child exists before preparation here.
inference_retirement_confirmed=true
cleanup() {
    local status=$? cleanup_status=0 capture_status=0 cancelled=0
    local native_cancellation=$inference_cancellation_status
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
        if [[ -n "$apply_capture" ]]; then
            if rm -rf -- "$apply_capture" 2>/dev/null; then apply_capture=; else
                capture_status=$?
                if [[ $status == 0 ]]; then status=$capture_status; fi
            fi
        fi
    fi
    trap 'caller_cancellation=129; exit 129' HUP
    trap 'caller_cancellation=130; exit 130' INT
    trap 'caller_cancellation=143; exit 143' TERM
    if [[ $cancelled != 0 ]]; then exit "$cancelled"; fi
    if [[ $caller_cancellation != 0 ]]; then exit "$caller_cancellation"; fi
    if [[ $native_cancellation != 0 ]]; then exit "$native_cancellation"; fi
    if ! "$inference_retirement_confirmed"; then
        # An unconfirmed owner forbids another helper; direct reporting retains
        # that owner's separate possible blocked-stderr limitation.
        printf '%s' "$inference_diagnostic" >&2
        if [[ -n "$docker_client_directory" ]]; then
            printf 'Cannot confirm package native client retirement; private Docker client state retained at %q.\n' "$docker_client_directory" >&2
        fi
        if [[ -n "$apply_capture" ]]; then
            printf 'Cannot confirm package native client retirement; private apply inputs retained at %q.\n' "$apply_capture" >&2
        fi
    elif [[ -n "$inference_diagnostic" || -n "$docker_client_directory" || -n "$apply_capture" ]]; then
        # A held native report avoids a full-pipe builtin write blocking
        # cancellation. Its new retirement fact authorizes no later cleanup.
        if inference_native 'package client cleanup reporting' untimed stdio bash -eu -c '
            printf "%s" "$1" >&2
            if [[ -n "$2" ]]; then
                printf "Cannot confirm package Docker client configuration removal; attempted path %q (status %s).\n" "$2" "$3" >&2
            fi
            if [[ -n "$4" ]]; then
                printf "Cannot confirm package apply input removal; attempted path %q (status %s).\n" "$4" "$5" >&2
            fi
        ' bash "$inference_diagnostic" "$docker_client_directory" "$cleanup_status" "$apply_capture" "$capture_status"; then :; else
            local report_status=$?
            if [[ $inference_cancellation_status != 0 ]]; then
                status=$inference_cancellation_status
            elif [[ $status == 0 ]]; then status=$report_status; fi
        fi
    fi
    exit "$status"
}
finish_controller_handoff() {
    local status=$?
    trap - EXIT
    if [[ $caller_cancellation != 0 ]]; then exit "$caller_cancellation"; fi
    if [[ $inference_cancellation_status != 0 ]]; then exit "$inference_cancellation_status"; fi
    # The parent client config is gone; outer completion conveys no inner fact.
    # Report only returned control text: no removal, helper, retry or fallback.
    # This direct write can block or fail; failed delivery must not replace the
    # controller's original status through errexit.
    if printf '%s' "$inference_diagnostic" >&2; then :; fi
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

if [[ "$1" == stage ]]; then
    input_capture="$stage_directory/.inputs"
else
    input_capture="$repository_root/.work/packages-stage/.apply.PRINT"
    if ! "$print_command"; then
        # This allocation returns the owned path to this shell. Suppress raw
        # allocation stderr before apply-path ownership is known; EXIT reports its status
        # after config cleanup. Generic filesystem hangs remain a separate limit.
        if apply_capture=$(umask 077; mktemp -d "$repository_root/.work/packages-stage/.apply.XXXXXXXX" 2>/dev/null); then :; else
            preparation_status=$?
            apply_capture=
            printf -v inference_diagnostic 'Cannot capture package apply inputs: private directory creation under %q failed (status %s).\n' \
                "$repository_root/.work/packages-stage" "$preparation_status"
            exit "$preparation_status"
        fi
        input_capture=$apply_capture
    fi
fi
if ! "$print_command"; then
    # One held owner captures utility/validation stderr and joins all capture
    # children before cleanup. Failures retain capped evidence with a controlled
    # summary; raw capture text is no longer written to public stderr here.
    inference_native 'package input capture' untimed discard bash -eu -c '
        repository_root=$1
        source "$repository_root/tools/package_inputs.sh"
        if [[ "$2" == stage ]]; then
            # This snapshot stays with the review stage on every outcome.
            (umask 077; mkdir -- "$3")
        fi
        package_capture_inputs "$2" "$3"
    ' bash "$repository_root" "$1" "$input_capture"
fi

if [[ "$1" == stage ]]; then
    # Bind the shared fixed BuildKit policy to its default builder, as in the
    # reviewed inference_image.sh build; ambient builders grant no authority.
    build=("${engine[@]}" build --builder default --quiet --network none --pull=false
        --build-arg "PYTHON_BASE=$python_image" --build-arg "UV_IMAGE=$uv_image"
        --file "$input_capture/context/containers/packages/Dockerfile.lock" "$input_capture/context")
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
            printf -v inference_diagnostic '%s\n' 'Cannot record package resolver image: observed local image identity differs from the built image ID.'
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
    command+=(--mount "type=bind,src=$input_capture/requirements/$name,dst=/input/$name,readonly")
done
if [[ "$1" == stage ]]; then
    # The retained capture is below the writable review tree. Hide that alias
    # behind one read-only child mount so /output cannot mutate admitted inputs.
    command+=(--mount "type=bind,src=$stage_directory,dst=/output"
        --mount "type=bind,src=$input_capture,dst=/output/.inputs,readonly")
else
    # Offline application mounts the reviewed tool and shared guard as modules.
    command+=(--mount "type=bind,src=$stage_directory,dst=/stage,readonly"
        --mount "type=bind,src=$repository_root/requirements,dst=/output"
        --workdir /workspace
        --mount "type=bind,src=$input_capture/context/tools/package_lock.py,dst=/workspace/tools/package_lock.py,readonly"
        --mount "type=bind,src=$input_capture/context/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly")
fi
if [[ "$1" == stage ]]; then
    command+=("$lock_image" stage)
else
    command+=("$python_image" python3 -B -m tools.package_lock apply)
fi
if [[ "$1" == stage ]]; then
    if "$print_command"; then
        "${command[@]}"
    else
        # The admitted build is retired, and its config is unused by docker.sh.
        # Finish this one removal before handoff, even if a signal arrives.
        trap 'caller_cancellation=129' HUP
        trap 'caller_cancellation=130' INT
        trap 'caller_cancellation=143' TERM
        if docker_client_cleanup 2>/dev/null; then handoff_cleanup_status=0; else
            handoff_cleanup_status=$?
            printf -v inference_diagnostic '%sCannot confirm package Docker client configuration removal; attempted path %q (status %s).\n' \
                "$inference_diagnostic" "$docker_client_directory" "$handoff_cleanup_status"
        fi
        # A failed removal is an attempted path, never authority for an EXIT
        # retry. Its text is presentation only; a signal keeps status priority.
        docker_client_directory=
        trap 'caller_cancellation=129; exit 129' HUP
        trap 'caller_cancellation=130; exit 130' INT
        trap 'caller_cancellation=143; exit 143' TERM
        if [[ $caller_cancellation != 0 ]]; then exit "$caller_cancellation"; fi
        if [[ $handoff_cleanup_status != 0 ]]; then exit "$handoff_cleanup_status"; fi
        trap finish_controller_handoff EXIT
        controller_native 'package stage controller' untimed stdio "${command[@]}"
        trap cleanup EXIT
    fi
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
