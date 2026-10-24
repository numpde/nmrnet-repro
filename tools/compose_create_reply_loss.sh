#!/usr/bin/env bash
# Qualification fault: let the real per-role Compose create finish, then lose
# its successful reply. Production recovery must adopt, never repeat, that create.
set -euo pipefail
"$@" > /dev/null
printf 'Qualification deliberately withheld the completed create reply.\n' >&2
exit 7
