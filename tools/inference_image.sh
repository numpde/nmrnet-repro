#!/usr/bin/env bash
# Build from verified local inputs offline; import only the recorded immutable image.
set -euo pipefail

readonly python_image='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly caller_uid="$(id -u)"
readonly caller_gid="$(id -g)"
readonly image_admission_timeout=30
source "$repository_root/tools/inference_native.sh"
source "$repository_root/tools/git_metadata.sh"
source "$repository_root/tools/docker_client.sh"
print_command=false
if [[ $# -gt 0 && "$1" == --print ]]; then print_command=true; shift; fi
native_timeout=
if [[ $# -gt 0 && "$1" == --timeout ]]; then
    if [[ $# -lt 2 || ! "$2" =~ ^([1-9][0-9]*(\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)$ ]]; then
        printf '%s\n' 'Inference --timeout requires positive finite seconds.' >&2
        exit 2
    fi
    native_timeout=$2
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
trap 'exit 129' HUP
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
docker_client_directory=
capture_directory=
temporary_record=
# Presentation only: EXIT publishes caller-owned failures after quiet cleanup.
inference_diagnostic=
inference_cancellation_status=0
caller_cancellation=0
# As in baseline.sh at d5684a5841d7c8df06a614ab38a8053e5252abad,
# the native helper owns this fact.
# Its unconfirmed retirement returns nonzero, so the bare calls below stop under
# errexit before another invocation can overwrite it. No native child exists yet.
inference_retirement_confirmed=true
cleanup() {
    local status=$? record_status=0 client_status=0 capture_status=0 cancelled=0
    local native_cancellation=$inference_cancellation_status
    trap - EXIT
    # Finish permitted removals before reporting: an unread public stderr pipe
    # can block diagnostics. Cancellation changes status without abandoning the
    # remaining removals; restore immediate cancellation before reporting.
    trap 'cancelled=129' HUP
    trap 'cancelled=130' INT
    trap 'cancelled=143' TERM
    if "$inference_retirement_confirmed"; then
        if [[ -n "$temporary_record" ]]; then
            if rm -f -- "$temporary_record" 2>/dev/null; then temporary_record=; else
                record_status=$?
                if [[ $status == 0 ]]; then status=$record_status; fi
            fi
        fi
        if docker_client_cleanup 2>/dev/null; then :; else
            client_status=$?
            if [[ $status == 0 ]]; then status=$client_status; fi
        fi
        if [[ -n "$capture_directory" ]]; then
            if rm -rf -- "$capture_directory" 2>/dev/null; then capture_directory=; else
                capture_status=$?
                if [[ $status == 0 ]]; then status=$capture_status; fi
            fi
        fi
    fi
    trap 'caller_cancellation=129; exit 129' HUP
    trap 'caller_cancellation=130; exit 130' INT
    trap 'caller_cancellation=143; exit 143' TERM
    # A cancellation received during removal must not start a report that can
    # block without another signal. An earlier native status 130 is distinct.
    if [[ $cancelled != 0 ]]; then exit "$cancelled"; fi
    if [[ $caller_cancellation != 0 ]]; then exit "$caller_cancellation"; fi
    if [[ $native_cancellation != 0 ]]; then exit "$native_cancellation"; fi
    if "$print_command"; then
        # PRINT owns no runtime client/config/capture. Keep its presentation
        # inert; blocking here cannot postpone removal of those resources.
        printf '%s' "$inference_diagnostic" >&2
    elif ! "$inference_retirement_confirmed"; then
        # Never replace an unconfirmed owner's fact with another native call.
        # These reports share that owner's possible blocked-stderr limitation.
        printf '%s' "$inference_diagnostic" >&2
        if [[ -n "$temporary_record" ]]; then
            printf 'Cannot confirm inference native client retirement; unpublished image record retained at %q.\n' "$temporary_record" >&2
        fi
        if [[ -n "$docker_client_directory" ]]; then
            printf 'Cannot confirm inference native client retirement; private Docker client state retained at %q.\n' "$docker_client_directory" >&2
        fi
        if [[ -n "$capture_directory" ]]; then
            printf 'Cannot confirm inference native client retirement; private build capture retained at %q.\n' "$capture_directory" >&2
        fi
    elif [[ -n "$inference_diagnostic" || -n "$temporary_record" || -n "$docker_client_directory" || -n "$capture_directory" ]]; then
        # Bash builtin printf cannot run cancellation traps during a full-pipe
        # write. Reuse the native owner's held anchor and join protocol for this
        # fixed report, after all removals; it reads only these explicit argv.
        # Its replacement fact cannot authorize further cleanup: exit follows.
        if inference_native 'cleanup reporting' untimed stdio bash -eu -c '
            printf "%s" "$1" >&2
            shift
            if [[ -n "$1" ]]; then
                printf "Cannot confirm unpublished inference image record removal; attempted path %q (status %s).\n" "$1" "$2" >&2
            fi
            if [[ -n "$3" ]]; then
                printf "Cannot confirm owned Docker client configuration removal; attempted path %q (status %s).\n" "$3" "$4" >&2
            fi
            if [[ -n "$5" ]]; then
                printf "Cannot confirm owned inference build capture removal; attempted path %q (status %s).\n" "$5" "$6" >&2
            fi
        ' bash "$inference_diagnostic" "$temporary_record" "$record_status" "$docker_client_directory" "$client_status" "$capture_directory" "$capture_status"; then :; else
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
if "$print_command"; then
    docker_client_prepare print
elif docker_client_prepare run; then :
else
    preparation_status=$?
    inference_diagnostic+=$docker_client_diagnostic
    exit "$preparation_status"
fi
if [[ "$1" == build ]]; then
    if [[ ! -d "$repository_root/requirements" || -L "$repository_root/requirements" ]]; then
        printf -v inference_diagnostic '%s\n' 'Cannot build inference image: requirements must be a directory without a symlink.'
        exit 2
    fi
    for input in inference.lock unicore-source.json; do
        if [[ ! -f "$repository_root/requirements/$input" || -L "$repository_root/requirements/$input" ]]; then
            printf -v inference_diagnostic 'Cannot build inference image: requirement input must be a regular file without a symlink: %s\n' "$input"
            exit 2
        fi
    done
    for directory in "$repository_root/.work/wheelhouse" "$repository_root/.work/wheelhouse/cpu"; do
        if [[ -L "$directory" || ! -d "$directory" ]]; then
            printf -v inference_diagnostic '%s\n' 'Cannot build inference image: verified .work/wheelhouse/cpu directory is missing or a symlink.'
            exit 2
        fi
    done
    receipt="$repository_root/.work/wheelhouse/cpu/.complete"
    if [[ ! -f "$receipt" || -L "$receipt" ]]; then
        printf -v inference_diagnostic '%s\n' 'Cannot build inference image: wheelhouse completion receipt is missing or a symlink.'
        exit 2
    fi
    if "$print_command"; then
        gitlink=$(repository_git -C "$repository_root" ls-files --stage -- NMRNetplusplus)
        read -r mode revision stage path <<< "$gitlink"
        if [[ "$mode" != 160000 || ! "$revision" =~ ^[0-9a-f]{40}$ || "$stage" != 0 \
              || "$(repository_git -C "$repository_root/NMRNetplusplus" rev-parse HEAD)" != "$revision" ]]; then
            printf -v inference_diagnostic '%s\n' 'Cannot build inference image: NMRNetplusplus must match its recorded submodule revision.'
            exit 2
        fi
        source_changes="$(repository_git -C "$repository_root/NMRNetplusplus" status --porcelain --untracked-files=all -- uninmr LICENSE)"
        source_untracked="$(repository_git -C "$repository_root/NMRNetplusplus" ls-files --others -- uninmr LICENSE)"
        if [[ -n "$source_changes" || -n "$source_untracked" ]]; then
            printf -v inference_diagnostic '%s\n' 'Cannot build inference image: uninmr and LICENSE must contain only unchanged pinned source.'
            exit 2
        fi
        frozen_context="$repository_root/.work/inference/.build.PRINT/context"
        frozen_wheelhouse="$repository_root/.work/inference/.build.PRINT/wheelhouse"
    else
        # The parent must learn the path it owns. Quiet setup diagnostics until
        # EXIT finishes permitted cleanup; generic filesystem hangs remain open.
        if mkdir -p -- "$repository_root/.work/inference" 2>/dev/null; then :; else
            preparation_status=$?
            printf -v inference_diagnostic 'Cannot capture inference inputs: work directory creation at %q failed (status %s).\n' \
                "$repository_root/.work/inference" "$preparation_status"
            exit "$preparation_status"
        fi
        for directory in "$repository_root/.work" "$repository_root/.work/inference"; do
            [[ $(stat -c '%u' -- "$directory" 2>/dev/null) == "$caller_uid" ]] \
                || { printf -v inference_diagnostic '%s\n' 'Cannot capture inference inputs: work directories must belong to the caller.'; exit 2; }
        done
        if capture_directory=$(umask 077; mktemp -d "$repository_root/.work/inference/.build.XXXXXXXX" 2>/dev/null); then :; else
            preparation_status=$?
            printf -v inference_diagnostic 'Cannot capture inference inputs: private build directory creation under %q failed (status %s).\n' \
                "$repository_root/.work/inference" "$preparation_status"
            exit "$preparation_status"
        fi
        # Bare -eu preserves capture failure propagation. The existing held
        # owner retains capped utility evidence and joins before caller cleanup.
        inference_native 'input capture' untimed discard bash -eu -c '
            repository_root=$1
            capture_directory=$2
            source "$repository_root/tools/inference_inputs.sh"
            inference_capture_inputs
        ' bash "$repository_root" "$capture_directory"
        # Capture already bound this declaration to its committed gitlink. Read
        # that authority, never live Git; raw comparison rejects read's NUL loss.
        declaration="$capture_directory/context/artifacts/nmrnet-source.paths"
        if [[ -f "$declaration" && ! -L "$declaration" ]] \
            && IFS= read -r source_revision_line 2>/dev/null < "$declaration" \
            && [[ "$source_revision_line" =~ ^source_revision\ [0-9a-f]{40}$ ]] \
            && cmp -s -- "$declaration" <(printf '%s\nLICENSE\nuninmr\n' "$source_revision_line" 2>/dev/null) 2>/dev/null; then
            revision=${source_revision_line#source_revision }
        else
            printf -v inference_diagnostic 'Cannot use captured inference source declaration at %q: expected one pinned revision followed by LICENSE and uninmr.\n' "$declaration"
            exit 2
        fi
        frozen_context="$capture_directory/context"
        frozen_wheelhouse="$capture_directory/wheelhouse"
    fi
    verify=("${engine[@]}" run --rm --init --network none --pull never --read-only
        --env NMRNET_IN_CONTAINER=1 --user "$caller_uid:$caller_gid"
        --cap-drop ALL --security-opt no-new-privileges:true
        --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --workdir /workspace
        --mount "type=bind,src=$frozen_context/tools/wheelhouse.py,dst=/workspace/tools/wheelhouse.py,readonly"
        --mount "type=bind,src=$frozen_context/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
        --mount "type=bind,src=$frozen_context/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
        --mount "type=bind,src=$frozen_context/requirements/inference.lock,dst=/input/inference.lock,readonly"
        --mount "type=bind,src=$frozen_context/requirements/unicore-source.json,dst=/input/unicore-source.json,readonly"
        --mount "type=bind,src=$frozen_wheelhouse,dst=/wheelhouse,readonly"
        "$python_image" python3 -B -m tools.wheelhouse check
        --lock /input/inference.lock --source /input/unicore-source.json --output /wheelhouse)
    if "$print_command"; then run "${verify[@]}"; else
        inference_native 'wheelhouse verification' 120 discard "${verify[@]}"
    fi
    if ! "$print_command"; then
        inference_native 'base image admission' "$image_admission_timeout" image-id \
            "${engine[@]}" image inspect --format '{{.Id}}' "$python_image"
    fi
    build=("${engine[@]}" build --builder default --quiet --network none --pull=false
        --build-arg "PYTHON_BASE=$python_image" --build-arg "NMRNET_REVISION=$revision"
        --build-context "wheelhouse=$frozen_wheelhouse"
        --file "$frozen_context/containers/inference/Dockerfile" "$frozen_context")
    if "$print_command"; then
        run "${build[@]}"
    else
        inference_native build "$native_timeout" image-id "${build[@]}"
        image_id=$inference_result
        inference_native 'built image admission' "$image_admission_timeout" image-id \
            "${engine[@]}" image inspect --format '{{.Id}}' "$image_id"
        if [[ "$inference_result" != "$image_id" ]]; then
            printf -v inference_diagnostic '%s\n' 'Cannot record inference image: observed local image identity differs from the built image ID.'
            exit 2
        fi
        temporary_record="$(mktemp "$repository_root/.work/inference/image-id.XXXXXXXX")"
        printf '%s\n' "$image_id" > "$temporary_record"
        mv -T -- "$temporary_record" "$image_record"
        temporary_record=
        # The native owner keeps a full public stdout pipe cancellable after
        # publication; the committed image record survives report cancellation.
        # Both success reports return child SIGPIPE as status 141 after cleanup;
        # the earlier builtin writes terminated the launcher itself by SIGPIPE.
        inference_native 'build success reporting' untimed stdio bash -eu -c \
            'printf "Built inference image %s\n" "$1"' bash "$image_id"
    fi
else
    if [[ ! -f "$image_record" ]]; then
        printf -v inference_diagnostic '%s\n' 'Cannot import inference stack: build the inference image first.'
        exit 2
    fi
    if [[ $(wc -c < "$image_record") != 72 ]] \
        || ! LC_ALL=C grep -a -x -E 'sha256:[0-9a-f]{64}' "$image_record" >/dev/null; then
        printf -v inference_diagnostic '%s\n' 'Cannot import inference stack: image-id does not contain an immutable image ID.'
        exit 2
    fi
    read -r image_id < "$image_record"
    if ! "$print_command"; then
        inference_native 'recorded image admission' "$image_admission_timeout" image-id \
            "${engine[@]}" image inspect --format '{{.Id}}' "$image_id"
        if [[ "$inference_result" != "$image_id" ]]; then
            printf -v inference_diagnostic '%s\n' 'Cannot import inference stack: observed local image identity differs from image-id.'
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
        inference_native 'import success reporting' untimed stdio bash -eu -c \
            'printf "%s\n" "$1"' bash 'Inference imports passed.'
    fi
fi
