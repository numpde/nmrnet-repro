#!/usr/bin/env bash
# Scientific checks consume Git objects, not checkout hooks, remotes or logs.
# https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_source.py
# This scaffold admits the supported checkout topology for two read-only object
# mounts. Those mounts expose committed history; they do not freeze host objects
# or provide a hostile-code sandbox. The caller owns their container grants.
set -euo pipefail

fail() { printf 'Cannot prepare checker Git inputs: %s\n' "$1" >&2; exit 2; }
[[ $# == 2 ]] || fail 'use check_git_inputs.sh REPOSITORY NEW_DIRECTORY.'
repository=$1
destination=$2
[[ -d "$repository" && ! -L "$repository" ]] || fail 'repository must be a directory without a symlink.'
[[ "$repository" == "$(cd -- "$repository" && pwd -P)" ]] || fail 'repository must use its canonical absolute path.'
parent=${destination%/*}
name=${destination##*/}
[[ "$destination" == /* && -n "$name" && "$name" != . && "$name" != .. && -d "$parent" && ! -L "$parent" ]] \
    || fail 'new metadata directory must have an existing canonical parent.'
[[ "$parent" == "$(cd -- "$parent" && pwd -P)" ]] || fail 'metadata parent must use its canonical absolute path.'
[[ ! -e "$destination" && ! -L "$destination" ]] || fail 'metadata destination already exists; preserve it.'
for variable in GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_REPLACE_REF_BASE GIT_NO_REPLACE_OBJECTS \
    GIT_NAMESPACE GIT_CONFIG GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS \
    GIT_CONFIG_SYSTEM GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM; do
    [[ ! -v "$variable" ]] || fail 'unset ambient Git source/configuration overrides before admitting repository objects.'
done
git_binary=$(command -v git) || fail 'Git is required for source identity admission.'
[[ "$git_binary" == /* && -f "$git_binary" && -x "$git_binary" ]] || fail 'Git must resolve to an executable file.'

parent_git="$repository/.git"
module_git="$parent_git/modules/nmrnet"
for directory in "$parent_git" "$parent_git/modules" "$module_git"; do
    [[ -d "$directory" && ! -L "$directory" ]] || fail 'supported .git/modules/nmrnet topology is missing or linked.'
    [[ ! -e "$directory/commondir" && ! -L "$directory/commondir" ]] || fail 'shared Git directories are not admitted.'
done
for database in "$parent_git" "$module_git"; do
    for file in HEAD config; do
        [[ -f "$database/$file" && ! -L "$database/$file" ]] || fail 'Git HEAD and config must be regular files without symlinks.'
    done
    for directory in objects refs; do
        [[ -d "$database/$directory" && ! -L "$database/$directory" ]] || fail 'Git objects and refs must be directories without symlinks.'
        unsafe=$(find "$database/$directory" -mindepth 1 \( -type l -o \( ! -type f ! -type d \) \) -print -quit) \
            || fail 'Git database structure could not be inspected.'
        [[ -z "$unsafe" ]] || fail 'Git object/ref trees contain a link or special file.'
    done
    [[ ! -e "$database/objects/info/alternates" && ! -L "$database/objects/info/alternates" ]] \
        || fail 'alternate object stores are not admitted.'
done
pointer="$repository/NMRNetplusplus/.git"
[[ -d "$repository/NMRNetplusplus" && ! -L "$repository/NMRNetplusplus" && -f "$pointer" && ! -L "$pointer" ]] \
    || fail 'upstream checkout and Git pointer must not be linked or special.'
cmp -s -- "$pointer" <(printf '%s\n' 'gitdir: ../.git/modules/nmrnet') \
    || fail 'upstream Git pointer differs from the supported local topology.'

read_git() {
    # Empty protocol admission overrides local promisor configuration, so missing
    # objects cannot start lazy fetches. These controls exist in Git 2.39:
    # https://git-scm.com/docs/git/2.39.0 (GIT_ALLOW_PROTOCOL, GIT_TERMINAL_PROMPT)
    # Native failures may contain private remote configuration. The caller's
    # phase-specific rejection below is the publishable diagnostic.
    env -i PATH=/usr/bin:/bin HOME=/nonexistent LC_ALL=C \
        GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
        GIT_ALLOW_PROTOCOL='' GIT_TERMINAL_PROMPT=0 \
        "$git_binary" --git-dir="$1" -c core.hooksPath=/dev/null -c core.fsmonitor=false "${@:2}" 2>/dev/null
}
parent_head=$(read_git "$parent_git" rev-parse --verify 'HEAD^{commit}') || fail 'parent commit could not be admitted.'
module_head=$(read_git "$module_git" rev-parse --verify 'HEAD^{commit}') || fail 'upstream commit could not be admitted.'
[[ "$parent_head" =~ ^[0-9a-f]{40}$ && "$module_head" =~ ^[0-9a-f]{40}$ ]] || fail 'Git revisions must be full commit IDs.'
gitlink=$(read_git "$parent_git" ls-tree "$parent_head" -- NMRNetplusplus) || fail 'committed upstream gitlink could not be read.'
[[ "$gitlink" == "160000 commit $module_head"$'\tNMRNetplusplus' ]] || fail 'upstream HEAD differs from the committed parent gitlink.'

# Every source admission completes before creating disposable metadata. An error
# after creation removes only this newly owned scaffold, never source Git state.
owned=false
cleanup() {
    status=$?
    if "$owned" && (( status != 0 )); then
        rm -rf -- "$destination" || printf '%s\n' 'Checker Git preparation failed; its metadata directory remains.' >&2
    fi
    exit "$status"
}
trap cleanup EXIT
mkdir -m 700 -- "$destination"
owned=true
mkdir -m 700 -- "$destination/objects" "$destination/refs" "$destination/modules"
mkdir -m 700 -- "$destination/modules/nmrnet"
mkdir -m 700 -- "$destination/modules/nmrnet/objects" "$destination/modules/nmrnet/refs"
printf '%s\n' "$parent_head" > "$destination/HEAD"
printf '%s\n' '[core]' 'repositoryformatversion = 0' 'bare = false' > "$destination/config"
printf '%s\n' "$module_head" > "$destination/modules/nmrnet/HEAD"
printf '%s\n' '[core]' 'repositoryformatversion = 0' 'bare = false' 'worktree = ../../../NMRNetplusplus' \
    > "$destination/modules/nmrnet/config"
chmod 600 -- "$destination/HEAD" "$destination/config" "$destination/modules/nmrnet/HEAD" "$destination/modules/nmrnet/config"
