#!/bin/bash
# Finite metadata responses for unit launchers; never fall back to native Git.
set -euo pipefail
for name in NMRNET_TEST_GIT_REPOSITORY NMRNET_TEST_GIT_PARENT_HEAD \
    NMRNET_TEST_GIT_UPSTREAM_HEAD NMRNET_TEST_GIT_STATE; do
    [[ -v "$name" ]] || exit 97
done
case "$NMRNET_TEST_GIT_STATE" in clean|dirty|failure) ;; *) exit 97 ;; esac
[[ $# -ge 8 && $1 == -c && $2 == core.hooksPath=/dev/null \
    && $3 == -c && $4 == core.fsmonitor=false && $5 == -C ]] || exit 97
repository=$6
shift 6
case "$repository" in
    "$NMRNET_TEST_GIT_REPOSITORY")
        case "$*" in
            'rev-parse --verify HEAD')
                [[ $# == 3 ]] || exit 97
                printf '%s\n' "$NMRNET_TEST_GIT_PARENT_HEAD" ;;
            'ls-files --stage -- NMRNetplusplus')
                [[ $# == 4 ]] || exit 97
                printf '160000 %s 0 NMRNetplusplus\n' "$NMRNET_TEST_GIT_UPSTREAM_HEAD" ;;
            *) exit 97 ;;
        esac ;;
    "$NMRNET_TEST_GIT_REPOSITORY/NMRNetplusplus")
        case "$*" in
            'rev-parse HEAD')
                [[ $# == 2 ]] || exit 97
                printf '%s\n' "$NMRNET_TEST_GIT_UPSTREAM_HEAD" ;;
            'status --porcelain --untracked-files=all -- uninmr LICENSE')
                [[ $# == 6 ]] || exit 97
                case "$NMRNET_TEST_GIT_STATE" in
                    clean) ;;
                    dirty) printf '%s\n' ' M uninmr/infer.py' ;;
                    failure) exit 1 ;;
                esac ;;
            'ls-files --others -- uninmr LICENSE')
                [[ $# == 5 ]] || exit 97 ;;
            *) exit 97 ;;
        esac ;;
    *) exit 97 ;;
esac
