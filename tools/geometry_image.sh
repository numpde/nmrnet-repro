#!/usr/bin/env bash
# Geometry-image operations share container.sh's client, native and cleanup
# owners. Committed-blob capture follows inference_inputs.sh; science stays offline.

geometry_image_inputs=(containers/geometry/Dockerfile containers/geometry/Dockerfile.dockerignore
    requirements/preparation.lock tools/artifacts.py tools/container_guard.py
    tools/rdkit_environment.py tools/wheelhouse.py
    requirements/provider-core-native-install.lock
    nmrnet_provider nmrnet_inference tools/provider_environment.py
    tools/baseline_prepare.py tools/container_resources.py)

capture_geometry_inputs() {
    local commit changes file
    source "$repository_root/tools/git_inputs.sh"
    commit=$(repository_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}')
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] \
        || { git_input_failure 'RDKit image inputs' 'HEAD is not a full commit ID.'; return 2; }
    changes=$(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${geometry_image_inputs[@]}")
    [[ -z "$changes" ]] \
        || { git_input_failure 'RDKit image inputs' 'build inputs must match their committed bytes and modes.'; return 2; }
    mkdir -- "$image_input_directory/context" "$image_input_directory/wheelhouse" "$image_input_directory/runtime-wheels"
    copy_git_blobs 'RDKit image inputs' "$repository_root" "$commit" "$image_input_directory/context" \
        "$image_input_directory/inputs" "${geometry_image_inputs[@]}"
    # copy_git_blobs admits every selected committed record. Required COPY and
    # installed-import consumers reject missing leaves; another ls-tree over
    # these selectors would omit the same missing paths and prove nothing more.
}

build_geometry_image() {
    local status image_id inputs_digest build_timeout=900
    local record="$repository_root/.work/preparation/image-id"
    if [[ -L "$record" || ( -e "$record" && ! -f "$record" ) ]]; then
        printf -v pending_diagnostic '%s\n' 'Cannot record RDKit image: image-id must be a regular file without a symlink.'
        exit 2
    fi
    if "$print_command"; then
        image_input_directory="$repository_root/.work/preparation/.build.PRINT"
        inputs_digest=CAPTURED_INPUTS_SHA256
    else
        if image_input_directory=$(umask 077; mktemp -d "$repository_root/.work/preparation/.build.XXXXXXXX" 2>/dev/null); then :; else
            status=$?
            image_input_directory=
            printf -v pending_diagnostic 'Cannot capture RDKit image inputs: private directory creation under %q failed (status %s).\n' \
                "$repository_root/.work/preparation" "$status"
            exit "$status"
        fi
        if container_native 'RDKit image input capture' untimed discard bash -eu -c '
            repository_root=$1 image_input_directory=$2 wheelhouse_directory=$3
            source "$repository_root/tools/geometry_image.sh"
            capture_geometry_inputs
        ' bash "$repository_root" "$image_input_directory" "$wheelhouse_directory"; then :; else
            status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
        fi
        inputs_digest=$(sha256sum -- "$image_input_directory/inputs")
        inputs_digest="sha256:${inputs_digest%% *}"
    fi
    source "$repository_root/tools/wheelhouse_inputs.sh"
    if "$print_command"; then
        print_wheel_capture_plan "$image_input_directory/context" preparation preparation.lock "$image_input_directory/wheelhouse" "$image"
    elif execute_wheel_capture "$image_input_directory/context" preparation preparation.lock "$image_input_directory/wheelhouse" "$image" "$build_timeout"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if "$print_command"; then
        print_wheel_capture_plan "$image_input_directory/context" provider-core provider-core-native-install.lock "$image_input_directory/runtime-wheels" "$image"
    elif execute_wheel_capture "$image_input_directory/context" provider-core provider-core-native-install.lock "$image_input_directory/runtime-wheels" "$image" "$build_timeout"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    local -a build=("${engine[@]}" build --builder default --quiet --network none --pull=false
        --build-arg "GEOMETRY_INPUTS=$inputs_digest"
        --build-context "wheelhouse=$image_input_directory/wheelhouse"
        --build-context "runtime-wheels=$image_input_directory/runtime-wheels"
        --file "$image_input_directory/context/containers/geometry/Dockerfile" "$image_input_directory/context")
    if "$print_command"; then printf '%q ' "${build[@]}"; printf '\n'; return; fi
    if container_native 'RDKit image build' "$build_timeout" image-id "${build[@]}"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    image_id=$inference_result
    if container_native 'RDKit built-image admission' 30 image-id "${engine[@]}" image inspect --format '{{.Id}}' "$image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if [[ "$inference_result" != "$image_id" ]]; then
        printf -v pending_diagnostic '%s\n' 'Cannot record RDKit image: the inspected identity differs from the build result.'
        exit 2
    fi
    require_geometry_inputs "$image_id" "$inputs_digest"
    printf '%s\n' "$image_id" > "$image_input_directory/image-id"
    mv -T -- "$image_input_directory/image-id" "$record"
    if container_native 'RDKit build reporting' untimed stdio bash -eu -c \
        'printf "Built RDKit worker image %s\n" "$1"' bash "$image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    source "$repository_root/tools/operator_guidance.sh"
    next_actions=$(next_make_command 'Next, build the controller image from the same verified dependency pool:' \
        provider/image/build "WHEELHOUSE=$wheelhouse_directory")
}

require_geometry_image() {
    local record="$repository_root/.work/preparation/image-id" status inputs_digest changes
    if [[ ! -f "$record" || -L "$record" ]] || [[ $(stat -c '%s' -- "$record") != 72 ]] \
        || ! LC_ALL=C grep -aExq 'sha256:[0-9a-f]{64}' "$record"; then
        printf -v pending_diagnostic '%s\n' 'Cannot run the RDKit worker: run make geometry/image/build to record its immutable image ID.'
        exit 2
    fi
    IFS= read -r geometry_image_id < "$record"
    if ! cmp -s -- "$record" <(printf '%s\n' "$geometry_image_id"); then
        printf -v pending_diagnostic '%s\n' 'Cannot run the RDKit worker: its image-id record is not byte-exact.'
        exit 2
    fi
    if "$print_command"; then return; fi
    if container_native 'RDKit recorded-image admission' 30 image-id "${engine[@]}" image inspect --format '{{.Id}}' "$geometry_image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if [[ "$inference_result" != "$geometry_image_id" ]]; then
        printf -v pending_diagnostic '%s\n' 'Cannot run the RDKit worker: the inspected identity differs from image-id.'
        exit 2
    fi
    # Bind the admitted image to this committed input closure, including modes
    # and lock hashes. Unrelated commits leave this Git-tree identity unchanged.
    source "$repository_root/tools/git_metadata.sh"
    changes=$(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${geometry_image_inputs[@]}")
    if [[ -n "$changes" ]]; then
        printf -v pending_diagnostic '%s\n' 'Cannot check the RDKit image: commit its build inputs, then run make geometry/image/build.'
        exit 2
    fi
    inputs_digest=$(repository_git -C "$repository_root" ls-tree -r -z HEAD -- "${geometry_image_inputs[@]}" | sha256sum)
    inputs_digest="sha256:${inputs_digest%% *}"
    require_geometry_inputs "$geometry_image_id" "$inputs_digest"
}

require_geometry_inputs() {
    local image_id=$1 expected_inputs=$2 status
    if container_native 'RDKit image-input admission' 30 image-id "${engine[@]}" image inspect \
        --format '{{index .Config.Labels "io.nmrnet-repro.geometry-inputs"}}' "$image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if [[ "$inference_result" != "$expected_inputs" ]]; then
        printf -v pending_diagnostic 'Cannot admit RDKit image: its build-input label is %s; committed inputs require %s. Run make geometry/image/build.\n' \
            "$inference_result" "$expected_inputs"
        exit 2
    fi
}
