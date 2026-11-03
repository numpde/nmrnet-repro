#!/bin/sh
# The isolated launcher fixture models a nonroot host caller even when the
# repository checker itself maps that caller to container root.
case "${1-}" in
    -u|-g) printf '%s\n' 1000 ;;
    *) exec /usr/bin/id "$@" ;;
esac
