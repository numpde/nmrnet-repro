#!/usr/bin/env bash
# Freeze the image's declared authority before any engine client sees it.
# This is the shell counterpart of the committed blob/mode proof in
# repository_checks/science/source.py (NMRPeak source/input precedent recorded there).
# https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_image_inputs.py
# Git metadata is read on the host; dependency verification stays contained.

inference_input_failure() {
    printf 'Cannot capture inference build inputs: %s\n' "$1" >&2
    return 2
}

inference_git() (
    # Disable checkout hooks, fsmonitor and lazy network fetches. The source
    # identity is the captured commit, never a replace-ref or caller Git override.
    local variable
    while IFS= read -r variable; do unset "$variable"; done < <(compgen -A variable GIT_)
    export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1
    export GIT_ALLOW_PROTOCOL='' GIT_TERMINAL_PROMPT=0
    "$inference_git_binary" -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"
)

inference_copy_blobs() {
    local repository=$1 commit=$2 destination=$3 inventory=$4
    shift 4
    local metadata name mode kind object_id ancestor part live_mode
    local -a parts
    inference_git -C "$repository" ls-tree -r -z "$commit" -- "$@" > "$inventory"
    : > "$inventory.paths"
    while IFS=$'\t' read -r -d '' metadata name; do
        read -r mode kind object_id <<< "$metadata"
        if [[ ! "$mode" =~ ^100(644|755)$ || "$kind" != blob || ! "$object_id" =~ ^[0-9a-f]{40}$ \
              || ! "$name" =~ ^[A-Za-z0-9_./-]+$ || "$name" == /* || "$name" == *'..'* ]]; then
            inference_input_failure 'committed inputs contain an invalid path or nonregular Git mode.'
            return 2
        fi
        # Even unchanged Git status does not prove a safe materialized ancestor.
        ancestor=$repository
        IFS=/ read -r -a parts <<< "$name"
        for part in "${parts[@]:0:${#parts[@]}-1}"; do
            ancestor+=/$part
            [[ -d "$ancestor" && ! -L "$ancestor" ]] \
                || { inference_input_failure 'live input traverses a missing or linked directory.'; return 2; }
        done
        [[ -f "$repository/$name" && ! -L "$repository/$name" ]] \
            || { inference_input_failure 'live input is missing, linked or special.'; return 2; }
        if [[ "$name" == */* ]]; then mkdir -p -- "$destination/${name%/*}"; fi
        inference_git -C "$repository" cat-file blob "$object_id" > "$destination/$name"
        chmod "${mode#100}" -- "$destination/$name"
        # Git owns executable bits, not effective execution permission: a noexec
        # mount must not erase an executable file's recorded mode.
        live_mode=$(stat -c '%a' -- "$repository/$name")
        if ! cmp -s -- "$repository/$name" "$destination/$name" \
            || { [[ "$mode" == 100755 ]] && (( (8#$live_mode & 8#111) == 0 )); } \
            || { [[ "$mode" == 100644 ]] && (( (8#$live_mode & 8#111) != 0 )); }; then
            inference_input_failure 'live input bytes or executable mode differ from the captured commit.'
            return 2
        fi
        printf '%s\n' "$name" >> "$inventory.paths"
    done < "$inventory"
    [[ -s "$inventory.paths" ]] || { inference_input_failure 'committed input inventory is empty.'; return 2; }
}

inference_capture_inputs() {
    local parent_head gitlink source_head changes untracked file digest name
    local -a authored=(artifacts/nmrnet-source.paths artifacts/nmrnet-source.sha256
        containers/inference/Dockerfile containers/inference/Dockerfile.dockerignore
        requirements/inference.lock requirements/unicore-source.json
        tools/artifacts.py tools/build_unicore.py tools/container_guard.py tools/wheelhouse.py)
    inference_git_binary=$(command -v git)
    [[ "$inference_git_binary" == /* && -f "$inference_git_binary" && -x "$inference_git_binary" ]] \
        || { inference_input_failure 'Git must resolve to an executable file.'; return 2; }
    parent_head=$(inference_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}')
    [[ "$parent_head" =~ ^[0-9a-f]{40}$ ]] \
        || { inference_input_failure 'parent HEAD is not a full commit ID.'; return 2; }
    source_head=$(inference_git -C "$repository_root/NMRNetplusplus" rev-parse --verify 'HEAD^{commit}')
    gitlink=$(inference_git -C "$repository_root" ls-tree "$parent_head" -- NMRNetplusplus)
    [[ "$source_head" =~ ^[0-9a-f]{40}$ && "$gitlink" == "160000 commit $source_head"$'\tNMRNetplusplus' ]] \
        || { inference_input_failure 'upstream HEAD differs from the committed parent gitlink.'; return 2; }
    revision=$source_head
    changes=$(inference_git -C "$repository_root" status --porcelain --untracked-files=all -- "${authored[@]}")
    untracked=$(inference_git -C "$repository_root" ls-files --others -- tools)
    [[ -z "$changes" && -z "$untracked" ]] \
        || { inference_input_failure 'authored inputs changed or tools contains untracked/ignored files.'; return 2; }
    changes=$(inference_git -C "$repository_root/NMRNetplusplus" status --porcelain --untracked-files=all -- LICENSE uninmr)
    untracked=$(inference_git -C "$repository_root/NMRNetplusplus" ls-files --others -- LICENSE uninmr)
    [[ -z "$changes" && -z "$untracked" ]] \
        || { inference_input_failure 'the complete pinned source closure changed.'; return 2; }

    mkdir -- "$capture_directory/context" "$capture_directory/wheelhouse"
    inference_copy_blobs "$repository_root" "$parent_head" "$capture_directory/context" "$capture_directory/authored" "${authored[@]}"
    cmp -s -- "$capture_directory/authored.paths" <(printf '%s\n' "${authored[@]}") \
        || { inference_input_failure 'committed authored inventory differs from the exact image inputs.'; return 2; }
    inference_git -C "$repository_root" cat-file blob "$parent_head:containers/inference/image-inputs.txt" > "$capture_directory/declared-inputs"
    [[ -f "$repository_root/containers/inference/image-inputs.txt" && ! -L "$repository_root/containers/inference/image-inputs.txt" ]] \
        && cmp -s -- "$repository_root/containers/inference/image-inputs.txt" "$capture_directory/declared-inputs" \
        || { inference_input_failure 'live image input declaration differs from its captured commit.'; return 2; }
    cmp -s -- "$capture_directory/authored.paths" "$capture_directory/declared-inputs" \
        || { inference_input_failure 'the committed image input declaration differs from its owner.'; return 2; }
    cmp -s -- "$capture_directory/context/artifacts/nmrnet-source.paths" \
        <(printf 'source_revision %s\nLICENSE\nuninmr\n' "$revision") \
        || { inference_input_failure 'source declaration differs from the captured gitlink.'; return 2; }
    mkdir -- "$capture_directory/context/NMRNetplusplus"
    inference_copy_blobs "$repository_root/NMRNetplusplus" "$revision" "$capture_directory/context/NMRNetplusplus" "$capture_directory/source" LICENSE uninmr
    : > "$capture_directory/manifest.paths"
    while IFS= read -r file; do
        digest=${file%%  *}
        name=${file#*  }
        [[ "$digest" =~ ^[0-9a-f]{64}$ && "$file" == "$digest  $name" && "$name" =~ ^[A-Za-z0-9_./-]+$ ]] \
            || { inference_input_failure 'the captured source manifest is malformed.'; return 2; }
        printf '%s\n' "$name" >> "$capture_directory/manifest.paths"
    done < "$capture_directory/context/artifacts/nmrnet-source.sha256"
    cmp -s -- "$capture_directory/source.paths" "$capture_directory/manifest.paths" \
        || { inference_input_failure 'the source manifest differs from the complete committed inventory.'; return 2; }
    (cd -- "$capture_directory/context/NMRNetplusplus"; sha256sum --check --strict ../artifacts/nmrnet-source.sha256 >/dev/null)

    # Copy only regular flat artifacts; the captured receipt and frozen verifier
    # prove the actual copied bytes. A concurrent writer can only cause rejection.
    local -a wheels
    shopt -s nullglob dotglob
    wheels=("$repository_root/.work/wheelhouse/cpu/"*)
    shopt -u nullglob dotglob
    for file in "${wheels[@]}"; do
        [[ -f "$file" && ! -L "$file" ]] \
            || { inference_input_failure 'wheelhouse contains a link, directory or special entry.'; return 2; }
        cp --no-dereference -- "$file" "$capture_directory/wheelhouse/"
        [[ -f "$capture_directory/wheelhouse/${file##*/}" && ! -L "$capture_directory/wheelhouse/${file##*/}" ]] \
            || { inference_input_failure 'wheelhouse entry changed to a link or special file while copying.'; return 2; }
        chmod 644 -- "$capture_directory/wheelhouse/${file##*/}"
    done
}
