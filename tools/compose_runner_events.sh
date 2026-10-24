#!/usr/bin/env bash
# Qualification projects only the runner's fixed lifecycle fields. Application
# output stays in bounded Docker logs; this is an observation, not an event store.
set -euo pipefail
identifier=$1
shift
[[ $identifier =~ ^[0-9a-f]{64}$ && $# -gt 0 ]] || exit 2
"$@" logs --tail 80 "$identifier" 2>&1 |
    while IFS= read -r line; do
        # Both application stderr and CLI errors use Docker's stderr stream.
        # Keep their bounded receiving route visible; retain only the projection.
        printf '%s\n' "$line" >&2
        if [[ $line =~ event=child_started\ operation=([0-9a-f]{32})\ pid=([1-9][0-9]*)$ ]]; then
            printf 'started\t%s\t%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
        elif [[ $line =~ event=child_retired\ operation=([0-9a-f]{32})\ returncode=(-?[0-9]+)\ reason=([a-z_]+)$ ]]; then
            printf 'retired\t%s\t%s\t%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}" "${BASH_REMATCH[3]}"
        fi
    done
