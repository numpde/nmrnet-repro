#!/usr/bin/env bash
# Read selected checkout metadata without caller Git overrides, hooks or fetches.
# Existing committed-input policy follows NMRPeak's source authority:
# https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_source.py
repository_git() (
    local variable git_binary
    # Resolve each query's PATH-owned executable, never an exported function.
    # Lookup failure keeps its status; an invalid resolution is an input error.
    if git_binary=$(command -v git); then :; else return "$?"; fi
    [[ "$git_binary" == /* && -f "$git_binary" && -x "$git_binary" ]] \
        || { printf '%s\n' 'Cannot read repository Git metadata: Git must resolve to an executable file.' >&2; return 2; }
    while IFS= read -r variable; do unset "$variable"; done < <(compgen -A variable GIT_)
    export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_NO_REPLACE_OBJECTS=1
    export GIT_ALLOW_PROTOCOL='' GIT_TERMINAL_PROMPT=0
    "$git_binary" -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"
)
