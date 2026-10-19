#!/usr/bin/env bash
# Geometry-image operations share container.sh's client, native and cleanup
# owners. Committed-blob capture follows inference_inputs.sh; science stays offline.

geometry_image_inputs=(containers/geometry/Dockerfile containers/geometry/Dockerfile.dockerignore
    requirements/preparation.lock tools/artifacts.py tools/container_guard.py
    tools/rdkit_environment.py tools/wheelhouse.py)

capture_geometry_inputs() {
    local commit changes file
    source "$repository_root/tools/git_inputs.sh"
    commit=$(repository_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}')
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] \
        || { git_input_failure 'RDKit image inputs' 'HEAD is not a full commit ID.'; return 2; }
    changes=$(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${geometry_image_inputs[@]}")
    [[ -z "$changes" ]] \
        || { git_input_failure 'RDKit image inputs' 'build inputs must match their committed bytes and modes.'; return 2; }
    mkdir -- "$image_input_directory/context" "$image_input_directory/wheelhouse"
    copy_git_blobs 'RDKit image inputs' "$repository_root" "$commit" "$image_input_directory/context" \
        "$image_input_directory/inputs" "${geometry_image_inputs[@]}"
    # Git sorts paths; require the entire requested closure, including absent leaves.
    cmp -s -- "$image_input_directory/inputs.paths" <(printf '%s\n' "${geometry_image_inputs[@]}" | LC_ALL=C sort) \
        || { git_input_failure 'RDKit image inputs' 'the committed input closure is incomplete.'; return 2; }
    local -a wheels
    shopt -s nullglob dotglob
    wheels=("$repository_root/.work/preparation/wheelhouse/"*)
    shopt -u nullglob dotglob
    for file in "${wheels[@]}"; do
        [[ -f "$file" && ! -L "$file" ]] \
            || { git_input_failure 'RDKit image inputs' 'wheelhouse contains a linked or nonregular entry.'; return 2; }
        cp --no-dereference -- "$file" "$image_input_directory/wheelhouse/"
        [[ -f "$image_input_directory/wheelhouse/${file##*/}" && ! -L "$image_input_directory/wheelhouse/${file##*/}" ]] \
            || { git_input_failure 'RDKit image inputs' 'a wheelhouse entry changed type while copying.'; return 2; }
        chmod 644 -- "$image_input_directory/wheelhouse/${file##*/}"
    done
}

build_geometry_image() {
    local status image_id inputs_digest
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
            repository_root=$1 image_input_directory=$2
            source "$repository_root/tools/geometry_image.sh"
            capture_geometry_inputs
        ' bash "$repository_root" "$image_input_directory"; then :; else
            status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
        fi
        inputs_digest=$(sha256sum -- "$image_input_directory/inputs")
        inputs_digest="sha256:${inputs_digest%% *}"
    fi
    local -a build=("${engine[@]}" build --builder default --quiet --network none --pull=false
        --build-arg "GEOMETRY_INPUTS=$inputs_digest"
        --build-context "wheelhouse=$image_input_directory/wheelhouse"
        --file "$image_input_directory/context/containers/geometry/Dockerfile" "$image_input_directory/context")
    if "$print_command"; then printf '%q ' "${build[@]}"; printf '\n'; return; fi
    if container_native 'RDKit image build' 900 image-id "${build[@]}"; then :; else
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
