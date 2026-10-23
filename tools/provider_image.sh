#!/usr/bin/env bash
# Installed-controller build/admission ports geometry_image.sh at 9c0d9b0.
# container.sh retains its original native-client and capture cleanup owners.
# Source/context labels identify committed inputs; immutable IDs identify output.

provider_image_inputs=(containers/provider/Dockerfile containers/provider/Dockerfile.dockerignore
    requirements/provider-core-build.lock requirements/provider-core.lock
    packages/provider-core/pyproject.toml
    nmrnet_inference/predict.py tools/artifacts.py tools/container_guard.py tools/provider_proxy.py tools/bound_http_proxy.py
    tools/provider_environment.py tools/provider_image.sh tools/wheelhouse.py)
# Both packages currently have flat module layouts. Every controller module is
# captured, while separate scientific entrypoints never enter this image.
for provider_input in "$repository_root"/nmrnet_provider/*.py "$repository_root"/packages/provider-core/src/provider_core/*.py; do
    case "$provider_input" in
        */nmrnet_provider/worker_main.py|*/nmrnet_provider/geometry_worker.py|*/nmrnet_provider/smiles_worker.py) continue ;;
    esac
    provider_image_inputs+=("${provider_input#"$repository_root"/}")
done
unset provider_input

capture_provider_inputs() {
    local commit changes file
    source "$repository_root/tools/git_inputs.sh"
    commit=$(repository_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}')
    [[ "$commit" =~ ^[0-9a-f]{40}$ ]] \
        || { git_input_failure 'controller image inputs' 'HEAD is not a full commit ID.'; return 2; }
    # Root selectors also detect deleted/added modules that a live glob misses.
    changes=$(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${provider_image_inputs[@]}" nmrnet_provider packages/provider-core/src)
    [[ -z "$changes" ]] \
        || { git_input_failure 'controller image inputs' 'build inputs must match their committed bytes and modes.'; return 2; }
    mkdir -- "$image_input_directory/context" "$image_input_directory/runtime-wheels" "$image_input_directory/build-wheels"
    copy_git_blobs 'controller image inputs' "$repository_root" "$commit" "$image_input_directory/context" \
        "$image_input_directory/inputs" "${provider_image_inputs[@]}"
    # Git sorts paths; require the entire requested closure, including absent leaves.
    cmp -s -- "$image_input_directory/inputs.paths" <(printf '%s\n' "${provider_image_inputs[@]}" | LC_ALL=C sort) \
        || { git_input_failure 'controller image inputs' 'the committed input closure is incomplete.'; return 2; }
    local -a wheels
    shopt -s nullglob dotglob
    wheels=("$repository_root/.work/provider-core/wheelhouse/"*)
    shopt -u nullglob dotglob
    for file in "${wheels[@]}"; do
        [[ -f "$file" && ! -L "$file" ]] \
            || { git_input_failure 'controller image inputs' 'wheelhouse contains a linked or nonregular entry.'; return 2; }
        cp --no-dereference -- "$file" "$image_input_directory/runtime-wheels/"
        [[ -f "$image_input_directory/runtime-wheels/${file##*/}" && ! -L "$image_input_directory/runtime-wheels/${file##*/}" ]] \
            || { git_input_failure 'controller image inputs' 'a wheelhouse entry changed type while copying.'; return 2; }
        chmod 644 -- "$image_input_directory/runtime-wheels/${file##*/}"
    done
    file=$repository_root/.work/wheelhouse/cpu/setuptools-84.0.0-py3-none-any.whl
    [[ -f $file && ! -L $file ]] \
        || { git_input_failure 'controller image inputs' 'the retained setuptools wheel must be a regular file without a symlink.'; return 2; }
    cp --no-dereference -- "$file" "$image_input_directory/build-wheels/"
    [[ -f $image_input_directory/build-wheels/${file##*/} && ! -L $image_input_directory/build-wheels/${file##*/} ]] \
        || { git_input_failure 'controller image inputs' 'the build wheel changed type while copying.'; return 2; }
    chmod 644 -- "$image_input_directory/build-wheels/${file##*/}"
}

build_provider_image() {
    local status image_id inputs_digest
    local record="$repository_root/.work/provider/image-id"
    if [[ -L "$record" || ( -e "$record" && ! -f "$record" ) ]]; then
        printf -v pending_diagnostic '%s\n' 'Cannot record controller image: image-id must be a regular file without a symlink.'
        exit 2
    fi
    if "$print_command"; then
        image_input_directory="$repository_root/.work/provider/.build.PRINT"
        inputs_digest=CAPTURED_INPUTS_SHA256
    else
        # RUN networking does not constrain FROM resolution. Admit the pinned
        # local base first, as inference_image.sh:259-260 does at 9c0d9b0; a
        # missing base belongs to the explicit default-route bootstrap target.
        if container_native 'controller base image admission' 30 image-id "${engine[@]}" image inspect --format '{{.Id}}' "$image"; then :; else
            status=$?
            pending_diagnostic+=$inference_diagnostic
            pending_diagnostic+='Controller base image is unavailable locally; run make packages/base-images/python/download explicitly before building. '
            exit "$status"
        fi
        if image_input_directory=$(umask 077; mktemp -d "$repository_root/.work/provider/.build.XXXXXXXX" 2>/dev/null); then :; else
            status=$?
            image_input_directory=
            printf -v pending_diagnostic 'Cannot capture controller image inputs: private directory creation under %q failed (status %s).\n' \
                "$repository_root/.work/provider" "$status"
            exit "$status"
        fi
        if container_native 'controller image input capture' untimed discard bash -eu -c '
            repository_root=$1 image_input_directory=$2
            source "$repository_root/tools/provider_image.sh"
            capture_provider_inputs
        ' bash "$repository_root" "$image_input_directory"; then :; else
            status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
        fi
        inputs_digest=$(sha256sum -- "$image_input_directory/inputs")
        inputs_digest="sha256:${inputs_digest%% *}"
    fi
    # Quiet build mode hides the failing RUN's cause. Keep capped plain logs and
    # admit the separate private ID file after the original client succeeds.
    local -a build=("${engine[@]}" build --builder default --progress plain --network none --pull=false
        --iidfile "$image_input_directory/built-image-id"
        --build-arg "PROVIDER_INPUTS=$inputs_digest"
        --build-context "runtime-wheels=$image_input_directory/runtime-wheels"
        --build-context "build-wheels=$image_input_directory/build-wheels"
        --file "$image_input_directory/context/containers/provider/Dockerfile" "$image_input_directory/context")
    if "$print_command"; then printf '%q ' "${build[@]}"; printf '\n'; return; fi
    if container_native 'controller image build' 900 discard "${build[@]}"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if [[ -f "$image_input_directory/built-image-id" && ! -L "$image_input_directory/built-image-id" ]] \
        && [[ $(stat -c '%s' -- "$image_input_directory/built-image-id") =~ ^7[12]$ ]] \
        && IFS= read -r -N 71 image_id < "$image_input_directory/built-image-id" \
        && [[ $image_id =~ ^sha256:[0-9a-f]{64}$ ]] \
        && { cmp -s -- "$image_input_directory/built-image-id" <(printf '%s' "$image_id") \
            || cmp -s -- "$image_input_directory/built-image-id" <(printf '%s\n' "$image_id"); }; then :; else
        pending_diagnostic='Cannot record provider controller image: the successful build must leave one complete immutable ID in its private regular ID file.'
        exit 2
    fi
    if container_native 'controller built-image admission' 30 image-id "${engine[@]}" image inspect --format '{{.Id}}' "$image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if [[ "$inference_result" != "$image_id" ]]; then
        printf -v pending_diagnostic '%s\n' 'Cannot record controller image: the inspected identity differs from the build result.'
        exit 2
    fi
    require_provider_inputs "$image_id" "$inputs_digest"
    printf '%s\n' "$image_id" > "$image_input_directory/image-id"
    mv -T -- "$image_input_directory/image-id" "$record"
    if container_native 'controller build reporting' untimed stdio bash -eu -c \
        'printf "Built provider controller image %s\n" "$1"' bash "$image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
}

require_provider_image() {
    local record="$repository_root/.work/provider/image-id" status inputs_digest changes
    if [[ ! -f "$record" || -L "$record" ]] || [[ $(stat -c '%s' -- "$record") != 72 ]] \
        || ! LC_ALL=C grep -aExq 'sha256:[0-9a-f]{64}' "$record"; then
        printf -v pending_diagnostic '%s\n' 'Cannot run the provider controller: run make provider/image/build to record its immutable image ID.'
        exit 2
    fi
    IFS= read -r provider_image_id < "$record"
    if ! cmp -s -- "$record" <(printf '%s\n' "$provider_image_id"); then
        printf -v pending_diagnostic '%s\n' 'Cannot run the provider controller: its image-id record is not byte-exact.'
        exit 2
    fi
    if "$print_command"; then return; fi
    if container_native 'controller recorded-image admission' 30 image-id "${engine[@]}" image inspect --format '{{.Id}}' "$provider_image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if [[ "$inference_result" != "$provider_image_id" ]]; then
        printf -v pending_diagnostic '%s\n' 'Cannot run the provider controller: the inspected identity differs from image-id.'
        exit 2
    fi
    # Bind the admitted image to this committed input closure, including modes
    # and lock hashes. Unrelated commits leave this Git-tree identity unchanged.
    source "$repository_root/tools/git_metadata.sh"
    changes=$(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${provider_image_inputs[@]}")
    if [[ -n "$changes" ]]; then
        printf -v pending_diagnostic '%s\n' 'Cannot check the controller image: commit its build inputs, then run make provider/image/build.'
        exit 2
    fi
    inputs_digest=$(repository_git -C "$repository_root" ls-tree -r -z HEAD -- "${provider_image_inputs[@]}" | sha256sum)
    inputs_digest="sha256:${inputs_digest%% *}"
    require_provider_inputs "$provider_image_id" "$inputs_digest"
}

require_provider_inputs() {
    local image_id=$1 expected_inputs=$2 status
    if container_native 'controller image-input admission' 30 image-id "${engine[@]}" image inspect \
        --format '{{index .Config.Labels "io.nmrnet-repro.provider-inputs"}}' "$image_id"; then :; else
        status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
    fi
    if [[ "$inference_result" != "$expected_inputs" ]]; then
        printf -v pending_diagnostic 'Cannot admit controller image: its build-input label is %s; committed inputs require %s. Run make provider/image/build.\n' \
            "$inference_result" "$expected_inputs"
        exit 2
    fi
}
