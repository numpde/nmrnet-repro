#!/usr/bin/env bash
# Freeze execution from Git while admitting editable dependency intent from the
# current worktree. The caller owns the capture lifetime and action boundary.
source "$repository_root/tools/git_inputs.sh"

package_capture_inputs() {
    local action=$1 destination=$2 profile=$3 parent_head name live_mode captured_mode
    local -a dependency_inputs
    case "$profile" in
        inference) dependency_inputs=(inference.in build.in unicore-source.json) ;;
        preparation) dependency_inputs=(preparation.in) ;;
        *) git_input_failure 'package dependency intent' 'unknown package profile.'; return 2 ;;
    esac
    local -a execution=(tools/container_guard.py tools/package_lock.py)
    if [[ $action == stage ]]; then
        execution=(containers/packages/Dockerfile.lock containers/packages/Dockerfile.lock.dockerignore "${execution[@]}")
    fi
    parent_head=$(repository_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}')
    [[ "$parent_head" =~ ^[0-9a-f]{40}$ ]] \
        || { git_input_failure 'package execution inputs' 'parent HEAD is not a full commit ID.'; return 2; }
    mkdir -- "$destination/context" "$destination/requirements"
    copy_git_blobs 'package execution inputs' "$repository_root" "$parent_head" \
        "$destination/context" "$destination/execution" "${execution[@]}"
    cmp -s -- "$destination/execution.paths" <(printf '%s\n' "${execution[@]}") \
        || { git_input_failure 'package execution inputs' 'committed inventory differs from the selected execution inputs.'; return 2; }

    # Admission is per invocation, not a transaction with editors across intent
    # files. Recheck physical paths while copying; earlier CLI checks cannot
    # authorize a later replacement. The worker hashes these copied bytes.
    for name in "${dependency_inputs[@]}"; do
        [[ -d "$repository_root/requirements" && ! -L "$repository_root/requirements" ]] \
            || { git_input_failure 'package dependency intent' 'requirements is missing or linked.'; return 2; }
        [[ -f "$repository_root/requirements/$name" && ! -L "$repository_root/requirements/$name" ]] \
            || { git_input_failure 'package dependency intent' "input $name is missing, linked or special."; return 2; }
        cp --preserve=mode --no-dereference -- "$repository_root/requirements/$name" "$destination/requirements/$name"
        [[ -d "$repository_root/requirements" && ! -L "$repository_root/requirements" \
           && -f "$destination/requirements/$name" && ! -L "$destination/requirements/$name" \
           && -f "$repository_root/requirements/$name" && ! -L "$repository_root/requirements/$name" ]] \
            || { git_input_failure 'package dependency intent' "input $name changed to a linked or special path while copying."; return 2; }
        live_mode=$(stat -c '%a' -- "$repository_root/requirements/$name")
        captured_mode=$(stat -c '%a' -- "$destination/requirements/$name")
        if ! cmp -s -- "$repository_root/requirements/$name" "$destination/requirements/$name" \
            || [[ "$live_mode" != "$captured_mode" ]]; then
            git_input_failure 'package dependency intent' "input $name bytes or mode changed while copying."
            return 2
        fi
    done
}
