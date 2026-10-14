#!/bin/bash
# A finite Git projection for engine-free launcher probes; no host Git effects.
set -euo pipefail
if [[ -n ${NMRNET_FAKE_GIT_STATUS:-} ]]; then exit "$NMRNET_FAKE_GIT_STATUS"; fi
case "$*" in
    *'ls-files --stage'*) printf '%s\n' '160000 aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa 0 NMRNetplusplus' ;;
    *'rev-parse HEAD'*|*'rev-parse --verify HEAD'*) printf '%s\n' aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
    *'status --porcelain'*)
        if [[ "$*" != *'/NMRNetplusplus '* && "${NMRNET_FAKE_DIRTY_ROOT:-0}" == 1 ]]; then
            printf '%s\n' ' M tools/wheelhouse.py'
        fi ;;
    *'ls-files --others'*)
        if [[ "$*" != *'/NMRNetplusplus '* && "${NMRNET_FAKE_IGNORED_OUTPUT:-0}" == 1 ]]; then
            printf '%s\n' 'tools/__pycache__/private.pyc'
        fi ;;
    *) printf 'unexpected fake git invocation: %s\n' "$*" >&2; exit 97 ;;
esac
