#!/usr/bin/env bash
# Controller image operations, ported from provider_image.sh at
# 765958643bd3371bc908218d09caaa73fcb406a4.
# Role wrappers supply literal roles, selectors and operator guidance;
# container.sh retains the native client, input capture and cleanup owners.

capture_core_inputs() {
    local description=$1 commit changes
    local -n selected_inputs=$2
    shift 2
    source "$repository_root/tools/git_inputs.sh"
    commit=$(repository_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}')
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] \
        || { git_input_failure "$description image inputs" 'HEAD is not a full commit ID.'; return 2; }
    changes=$(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${selected_inputs[@]}" "$@")
    [[ -z "$changes" ]] \
        || { git_input_failure "$description image inputs" 'build inputs must match their committed bytes and modes.'; return 2; }
    mkdir -- "$image_input_directory/context" "$image_input_directory/runtime-wheels"
    copy_git_blobs "$description image inputs" "$repository_root" "$commit" "$image_input_directory/context" \
        "$image_input_directory/inputs" "${selected_inputs[@]}"
    cmp -s -- "$image_input_directory/inputs.paths" <(printf '%s\n' "${selected_inputs[@]}" | LC_ALL=C sort) \
        || { git_input_failure "$description image inputs" 'the committed input closure is incomplete.'; return 2; }
}

build_core_image() {
    local kind=$1 description=$2 status image_id inputs_digest build_timeout=900
    local record="$repository_root/.work/$kind/image-id"
    if [[ -L "$record" || ( -e "$record" && ! -f "$record" ) ]]; then
        printf -v pending_diagnostic 'Cannot record %s image: image-id must be a regular file without a symlink.\n' "$description"
        exit 2
    fi
    if "$print_command"; then
        image_input_directory="$repository_root/.work/$kind/.build.PRINT"
        inputs_digest=CAPTURED_INPUTS_SHA256
    else
        # FROM resolution is outside RUN's network grant. A missing base belongs
        # to explicit bootstrap, never an implicit pull during an offline build.
        if container_native "$description base image admission" 30 image-id "${engine[@]}" image inspect --format '{{.Id}}' "$image"; then :; else
            status=$?; pending_diagnostic+=$inference_diagnostic
            pending_diagnostic+='Pinned Python base image is unavailable locally; run make packages/base-images/python/download explicitly before building. '
            exit "$status"
        fi
        if image_input_directory=$(umask 077; mktemp -d "$repository_root/.work/$kind/.build.XXXXXXXX" 2>/dev/null); then :; else
            status=$?; image_input_directory=
            printf -v pending_diagnostic 'Cannot capture %s image inputs: private directory creation under %q failed (status %s).\n' \
                "$description" "$repository_root/.work/$kind" "$status"
            exit "$status"
        fi
        if container_native "$description image input capture" untimed discard bash -eu -c '
            repository_root=$1 image_input_directory=$2 wheelhouse_directory=$3 image_kind=$4
            source "$repository_root/tools/${image_kind}_image.sh"
            "capture_${image_kind}_inputs"
        ' bash "$repository_root" "$image_input_directory" "$wheelhouse_directory" "$kind"; then :; else
            status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
        fi
        inputs_digest=$(sha256sum -- "$image_input_directory/inputs")
        inputs_digest="sha256:${inputs_digest%% *}"
    fi
    source "$repository_root/tools/wheelhouse_inputs.sh"
    if "$print_command"; then
        print_wheel_capture_plan "$image_input_directory/context" provider-core provider-core-install.lock "$image_input_directory/runtime-wheels" "$image"
    elif execute_wheel_capture "$image_input_directory/context" provider-core provider-core-install.lock "$image_input_directory/runtime-wheels" "$image" "$build_timeout"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    local -a build=("${engine[@]}" build --builder default --progress plain --network none --pull=false
        --iidfile "$image_input_directory/built-image-id" --build-arg "${kind^^}_INPUTS=$inputs_digest"
        --build-context "runtime-wheels=$image_input_directory/runtime-wheels"
        --file "$image_input_directory/context/containers/$kind/Dockerfile" "$image_input_directory/context")
    if "$print_command"; then printf '%q ' "${build[@]}"; printf '\n'; return; fi
    # Only committed public source and locked wheels enter this build. Inherit
    # its progress/failure streams; the immutable ID still comes from its file.
    if container_native "$description image build" "$build_timeout" stdio "${build[@]}"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if [[ -f "$image_input_directory/built-image-id" && ! -L "$image_input_directory/built-image-id" ]] \
        && [[ $(stat -c '%s' -- "$image_input_directory/built-image-id") =~ ^7[12]$ ]] \
        && IFS= read -r -N 71 image_id < "$image_input_directory/built-image-id" \
        && [[ $image_id =~ ^sha256:[0-9a-f]{64}$ ]] \
        && { cmp -s -- "$image_input_directory/built-image-id" <(printf '%s' "$image_id") \
            || cmp -s -- "$image_input_directory/built-image-id" <(printf '%s\n' "$image_id"); }; then :; else
        printf -v pending_diagnostic 'Cannot record %s image: the successful build must leave one complete immutable ID in its private regular ID file.\n' "$description"
        exit 2
    fi
    if container_native "$description built-image admission" 30 image-id "${engine[@]}" image inspect --format '{{.Id}}' "$image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if [[ "$inference_result" != "$image_id" ]]; then
        printf -v pending_diagnostic 'Cannot record %s image: the inspected identity differs from the build result.\n' "$description"
        exit 2
    fi
    require_core_inputs "$kind" "$description" "$image_id" "$inputs_digest"
    printf '%s\n' "$image_id" > "$image_input_directory/image-id"
    mv -T -- "$image_input_directory/image-id" "$record"
    if container_native "$description build reporting" untimed stdio bash -eu -c \
        'printf "Built %s image %s\n" "$1" "$2"' bash "$description" "$image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
}

require_core_image() {
    local kind=$1 description=$2 status inputs_digest changes image_id
    local -n selected_inputs=$3
    local record="$repository_root/.work/$kind/image-id"
    if [[ ! -f "$record" || -L "$record" ]] || [[ $(stat -c '%s' -- "$record") != 72 ]] \
        || ! LC_ALL=C grep -aExq 'sha256:[0-9a-f]{64}' "$record"; then
        printf -v pending_diagnostic 'Cannot run %s: run make %s/image/build to record its immutable image ID.\n' "$description" "$kind"
        exit 2
    fi
    IFS= read -r image_id < "$record"
    if ! cmp -s -- "$record" <(printf '%s\n' "$image_id"); then
        printf -v pending_diagnostic 'Cannot run %s: its image-id record is not byte-exact.\n' "$description"
        exit 2
    fi
    core_image_id=$image_id
    if "$print_command"; then return; fi
    if container_native "$description recorded-image admission" 30 image-id "${engine[@]}" image inspect --format '{{.Id}}' "$image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if [[ "$inference_result" != "$image_id" ]]; then
        printf -v pending_diagnostic 'Cannot run %s: the inspected identity differs from image-id.\n' "$description"
        exit 2
    fi
    source "$repository_root/tools/git_metadata.sh"
    changes=$(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${selected_inputs[@]}")
    if [[ -n "$changes" ]]; then
        printf -v pending_diagnostic 'Cannot check %s image: commit its build inputs, then run make %s/image/build.\n' "$description" "$kind"
        exit 2
    fi
    inputs_digest=$(repository_git -C "$repository_root" ls-tree -r -z HEAD -- "${selected_inputs[@]}" | sha256sum)
    inputs_digest="sha256:${inputs_digest%% *}"
    require_core_inputs "$kind" "$description" "$image_id" "$inputs_digest"
}

require_core_inputs() {
    local kind=$1 description=$2 image_id=$3 expected_inputs=$4 status
    if container_native "$description image-input admission" 30 image-id "${engine[@]}" image inspect \
        --format "{{index .Config.Labels \"io.nmrnet-repro.$kind-inputs\"}}" "$image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if [[ "$inference_result" != "$expected_inputs" ]]; then
        printf -v pending_diagnostic 'Cannot admit %s image: its build-input label is %s; committed inputs require %s. Run make %s/image/build.\n' \
            "$description" "$inference_result" "$expected_inputs" "$kind"
        exit 2
    fi
}
