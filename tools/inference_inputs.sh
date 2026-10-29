#!/usr/bin/env bash
# Freeze the image's declared authority before any engine client sees it.
# This is the shell counterpart of the committed blob/mode proof in
# repository_checks/science/source.py (NMRPeak source/input precedent recorded there).
# https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_image_inputs.py
# Git metadata is read on the host; dependency verification stays contained.

source "$repository_root/tools/git_inputs.sh"

inference_capture_inputs() {
    local parent_head gitlink source_head source_root changes file digest name
    local -a authored=()
    parent_head=$(repository_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}')
    [[ "$parent_head" =~ ^[0-9a-f]{40}$ ]] \
        || { git_input_failure 'inference build inputs' 'parent HEAD is not a full commit ID.'; return 2; }
    # This committed declaration is the selection owner, not a second list
    # mirrored in shell/Python. Package roots expand through committed Git blobs.
    repository_git -C "$repository_root" cat-file blob "$parent_head:containers/inference/image-inputs.txt" > "$capture_directory/declared-inputs"
    [[ -f "$repository_root/containers/inference/image-inputs.txt" && ! -L "$repository_root/containers/inference/image-inputs.txt" ]] \
        && cmp -s -- "$repository_root/containers/inference/image-inputs.txt" "$capture_directory/declared-inputs" \
        || { git_input_failure 'inference build inputs' 'live image input declaration differs from its captured commit.'; return 2; }
    mapfile -t authored < "$capture_directory/declared-inputs"
    [[ ${#authored[@]} -gt 0 ]] && cmp -s -- "$capture_directory/declared-inputs" <(printf '%s\n' "${authored[@]}" | LC_ALL=C sort -u) \
        || { git_input_failure 'inference build inputs' 'selectors must be nonempty, sorted, unique complete lines.'; return 2; }
    for file in "${authored[@]}"; do
        [[ $file =~ ^[A-Za-z0-9_./-]+$ && $file != /* && /$file/ != */../* && /$file/ != */./* ]] \
            || { git_input_failure 'inference build inputs' 'selectors must be canonical repository-relative source paths.'; return 2; }
    done
    source_root=$(repository_git -C "$repository_root/NMRNetplusplus" rev-parse --show-toplevel)
    if [[ "$source_root" != "$repository_root/NMRNetplusplus" ]]; then
        if [[ "$source_root" == "$repository_root" ]]; then
            git_input_failure 'inference build inputs' 'the upstream source submodule is not initialized; run git submodule update --init --recursive.'
        else
            git_input_failure 'inference build inputs' 'the upstream source path is not the root of its own Git repository.'
        fi
        return 2
    fi
    source_head=$(repository_git -C "$repository_root/NMRNetplusplus" rev-parse --verify 'HEAD^{commit}')
    gitlink=$(repository_git -C "$repository_root" ls-tree "$parent_head" -- NMRNetplusplus)
    [[ "$source_head" =~ ^[0-9a-f]{40}$ && "$gitlink" == "160000 commit $source_head"$'\tNMRNetplusplus' ]] \
        || { git_input_failure 'inference build inputs' 'upstream HEAD differs from the committed parent gitlink.'; return 2; }
    revision=$source_head
    # Ignored files are outside the committed blob inventory copied below.
    changes=$(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${authored[@]}")
    [[ -z "$changes" ]] \
        || { git_input_failure 'inference build inputs' 'authored inputs changed.'; return 2; }
    changes=$(repository_git -C "$repository_root/NMRNetplusplus" status --porcelain --untracked-files=all -- LICENSE uninmr)
    [[ -z "$changes" ]] \
        || { git_input_failure 'inference build inputs' 'the complete pinned source closure changed.'; return 2; }

    mkdir -- "$capture_directory/context" "$capture_directory/wheelhouse" "$capture_directory/runtime-wheels"
    copy_git_blobs 'inference build inputs' "$repository_root" "$parent_head" "$capture_directory/context" "$capture_directory/authored" "${authored[@]}"
    cmp -s -- "$capture_directory/context/artifacts/nmrnet-source.paths" \
        <(printf 'source_revision %s\nLICENSE\nuninmr\n' "$revision") \
        || { git_input_failure 'inference build inputs' 'source declaration differs from the captured gitlink.'; return 2; }
    mkdir -- "$capture_directory/context/NMRNetplusplus"
    copy_git_blobs 'inference build inputs' "$repository_root/NMRNetplusplus" "$revision" "$capture_directory/context/NMRNetplusplus" "$capture_directory/source" LICENSE uninmr
    : > "$capture_directory/manifest.paths"
    while IFS= read -r file; do
        digest=${file%%  *}
        name=${file#*  }
        [[ "$digest" =~ ^[0-9a-f]{64}$ && "$file" == "$digest  $name" && "$name" =~ ^[A-Za-z0-9_./-]+$ ]] \
            || { git_input_failure 'inference build inputs' 'the captured source manifest is malformed.'; return 2; }
        printf '%s\n' "$name" >> "$capture_directory/manifest.paths"
    done < "$capture_directory/context/artifacts/nmrnet-source.sha256"
    cmp -s -- "$capture_directory/source.paths" "$capture_directory/manifest.paths" \
        || { git_input_failure 'inference build inputs' 'the source manifest differs from the complete committed inventory.'; return 2; }
    (cd -- "$capture_directory/context/NMRNetplusplus"; sha256sum --check --strict ../artifacts/nmrnet-source.sha256 >/dev/null)

}
