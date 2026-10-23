#!/usr/bin/env bash
# Source-only command formatting. Workflow owners choose the action and publish
# it only after their successful cleanup; text never selects or executes work.

next_make_command() {
    local purpose=$1
    shift
    printf '\n%s\n  make' "$purpose"
    printf ' %q' "$@"
    printf '\n'
}

report_next_actions() {
    # The combined preparation command supplies one final follow-up instead.
    if [[ ${NMRNET_SUPPRESS_NEXT_ACTIONS-} == 1 ]]; then return 0; fi
    # stderr keeps operator guidance separate from machine-readable stdout.
    inference_native 'next-action reporting' untimed stdio bash -eu -c \
        'printf "%s\n" "$1" >&2' bash "$1"
}
