#!/bin/sh
# Only ownership projections need the fixture's caller translation.
if [ "$#" -ge 2 ] && [ "$1" = -c ]; then
    case "$2" in
        %u|%u:%g) exec python3 -B "$(dirname -- "$(readlink -f -- "$0")")/stat.py" "$@" ;;
    esac
fi
exec /usr/bin/stat "$@"
