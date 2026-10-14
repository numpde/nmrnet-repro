#!/bin/bash
# A finite Git projection for engine-free launcher probes; no host Git effects.
set -euo pipefail
if [[ -n ${NMRNET_FAKE_GIT_STATUS:-} ]]; then exit "$NMRNET_FAKE_GIT_STATUS"; fi
# Consume only the native admission options used by this launcher. Unknown
# invocations still reach the rejection below; there is no real Git fallback.
if [[ ${1:-} == -c ]]; then
    [[ ${2:-} == core.hooksPath=/dev/null && ${3:-} == -c && ${4:-} == core.fsmonitor=false ]] || exit 97
    shift 4
fi
[[ ${1:-} == -C && $# -ge 3 ]] || { printf '%s\n' 'fake Git requires one checkout'; exit 97; }
repository=$2
shift 2
if [[ "$repository" == */NMRNetplusplus ]]; then
    database="${repository%/NMRNetplusplus}/.fake-git/module"
else
    database="$repository/.fake-git/parent"
fi
[[ -f "$database/HEAD" ]] || exit 97
head=$(< "$database/HEAD")
mapfile -t selection < "$database/selection.paths"
case "$*" in
    'ls-tree -r -z '* )
        [[ $# == $((${#selection[@]} + 5)) && $1 == ls-tree && $2 == -r && $3 == -z && $4 == "$head" && $5 == -- ]] || exit 97
        cmp -s -- "$database/selection.paths" <(printf '%s\n' "${@:6}") || exit 97
        cat -- "$database/tree" ;;
    "ls-tree $head -- NMRNetplusplus")
        [[ "$database" == */parent && $# == 4 && $1 == ls-tree && $2 == "$head" && $3 == -- && $4 == NMRNetplusplus ]] || exit 97
        cat -- "$database/gitlink" ;;
    "cat-file blob $head:containers/inference/image-inputs.txt")
        [[ "$database" == */parent && $# == 3 && $1 == cat-file && $2 == blob ]] || exit 97
        cat -- "$database/declared-inputs" ;;
    'cat-file blob '*)
        object_id=${3:-}
        [[ $# == 3 && $1 == cat-file && $2 == blob && "$object_id" =~ ^[0-9a-f]{40}$ && -f "$database/$object_id" ]] || exit 97
        cat -- "$database/$object_id" ;;
    'ls-files --stage -- NMRNetplusplus')
        [[ "$database" == */parent && $# == 4 && $1 == ls-files && $2 == --stage && $3 == -- ]] || exit 97
        cat -- "$database/index-gitlink" ;;
    'rev-parse HEAD'|'rev-parse --verify HEAD'|'rev-parse --verify HEAD^{commit}')
        [[ $1 == rev-parse && ( ( $# == 2 && $2 == HEAD ) \
            || ( $# == 3 && $2 == --verify && ( $3 == HEAD || $3 == 'HEAD^{commit}' ) ) ) ]] || exit 97
        printf '%s\n' "$head" ;;
    'status --porcelain --untracked-files=all -- '*)
        [[ $1 == status && ${2:-} == --porcelain && ${3:-} == --untracked-files=all && ${4:-} == -- ]] || exit 97
        if [[ "$database" == */parent ]]; then
            [[ $# == $((${#selection[@]} + 4)) ]] || exit 97
            cmp -s -- "$database/selection.paths" <(printf '%s\n' "${@:5}") || exit 97
        else
            [[ $# == 6 && ( ( $5 == LICENSE && $6 == uninmr ) || ( $5 == uninmr && $6 == LICENSE ) ) ]] || exit 97
        fi
        if [[ "$repository" != */NMRNetplusplus && "${NMRNET_FAKE_DIRTY_ROOT:-0}" == 1 ]]; then
            printf '%s\n' ' M tools/wheelhouse.py'
        fi ;;
    'ls-files --others -- tools'|'ls-files --others -- LICENSE uninmr'|'ls-files --others -- uninmr LICENSE')
        [[ $1 == ls-files && ${2:-} == --others && ${3:-} == -- && ( $# == 4 || $# == 5 ) ]] || exit 97
        if [[ "$database" == */parent ]]; then [[ $# == 4 && $4 == tools ]] || exit 97
        else [[ $# == 5 && ( ( $4 == LICENSE && $5 == uninmr ) || ( $4 == uninmr && $5 == LICENSE ) ) ]] || exit 97; fi
        if [[ "$repository" != */NMRNetplusplus && "${NMRNET_FAKE_IGNORED_OUTPUT:-0}" == 1 ]]; then
            printf '%s\n' 'tools/__pycache__/private.pyc'
        fi ;;
    *) printf 'unexpected fake git invocation: %s\n' "$*" >&2; exit 97 ;;
esac
