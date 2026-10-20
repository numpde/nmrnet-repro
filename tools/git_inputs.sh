#!/usr/bin/env bash
# Materialize selected committed blobs with their Git executable modes.
# Callers own the selected closure and supply its required diagnostic label.
# Existing NMRPeak committed-input policy is recorded by inference_inputs.sh.
source "$repository_root/tools/git_metadata.sh" || return $?

git_input_failure() {
    printf 'Cannot capture %s: %s\n' "$1" "$2" >&2
    return 2
}

copy_git_blobs() {
    local operation=$1 repository=$2 commit=$3 destination=$4 inventory=$5
    shift 5
    local metadata name mode kind object_id ancestor part live_mode
    local -a parts
    # Callers may invoke capture in a conditional, which disables errexit inside
    # this function. Every effect must propagate its own failure explicitly.
    repository_git -C "$repository" ls-tree -r -z "$commit" -- "$@" > "$inventory" || return $?
    : > "$inventory.paths" || return $?
    while IFS=$'\t' read -r -d '' metadata name; do
        read -r mode kind object_id <<< "$metadata"
        if [[ ! "$mode" =~ ^100(644|755)$ || "$kind" != blob || ! "$object_id" =~ ^[0-9a-f]{40}$ \
              || ! "$name" =~ ^[A-Za-z0-9_./-]+$ || "$name" == /* || "$name" == *'..'* ]]; then
            git_input_failure "$operation" 'committed inputs contain an invalid path or nonregular Git mode.'
            return 2
        fi
        # Even unchanged Git status does not prove a safe materialized ancestor.
        ancestor=$repository
        IFS=/ read -r -a parts <<< "$name"
        for part in "${parts[@]:0:${#parts[@]}-1}"; do
            ancestor+=/$part
            [[ -d "$ancestor" && ! -L "$ancestor" ]] \
                || { git_input_failure "$operation" 'live input traverses a missing or linked directory.'; return 2; }
        done
        [[ -f "$repository/$name" && ! -L "$repository/$name" ]] \
            || { git_input_failure "$operation" 'live input is missing, linked or special.'; return 2; }
        # Git records file modes, not directory permissions. The inference
        # Dockerfile copies source subdirectories into a nonroot image: they
        # need readable/traversable 0755 ancestors for Python discovery. Keep
        # the caller's pre-existing private capture root (0700) unchanged.
        # containers/inference/Dockerfile: COPY NMRNetplusplus/uninmr/ ...
        if [[ "$name" == */* ]]; then (umask 022; mkdir -p -- "$destination/${name%/*}") || return $?; fi
        repository_git -C "$repository" cat-file blob "$object_id" > "$destination/$name" || return $?
        chmod "${mode#100}" -- "$destination/$name" || return $?
        # Git owns executable bits, not effective execution permission: a noexec
        # mount must not erase an executable file's recorded mode.
        live_mode=$(stat -c '%a' -- "$repository/$name") || return $?
        if ! cmp -s -- "$repository/$name" "$destination/$name" \
            || { [[ "$mode" == 100755 ]] && (( (8#$live_mode & 8#111) == 0 )); } \
            || { [[ "$mode" == 100644 ]] && (( (8#$live_mode & 8#111) != 0 )); }; then
            git_input_failure "$operation" 'live input bytes or executable mode differ from the captured commit.'
            return 2
        fi
        printf '%s\n' "$name" >> "$inventory.paths" || return $?
    done < "$inventory"
    [[ -s "$inventory.paths" ]] || { git_input_failure "$operation" 'committed input inventory is empty.'; return 2; }
}
