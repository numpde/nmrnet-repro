#!/usr/bin/env bash
# Qualification fault: let the real Engine command finish, then lose its reply.
# Callers exercise fixed create/start/stop effects; this is never a provider path.
set -euo pipefail
"$@" > /dev/null
printf 'Qualification deliberately withheld the completed Engine reply.\n' >&2
exit 7
