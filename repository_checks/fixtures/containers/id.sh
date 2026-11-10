#!/bin/sh
# The isolated launcher fixture models a nonroot host caller even when the
# repository checker itself maps that caller to container root.
if [ "$#" -eq 1 ]; then
    case "$1" in
        -u) printf '%s\n' "${NMRNET_FIXTURE_UID:?}"; exit ;;
        -g) printf '%s\n' "${NMRNET_FIXTURE_GID:?}"; exit ;;
    esac
fi
exec /usr/bin/id "$@"
