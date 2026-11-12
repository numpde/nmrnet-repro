#!/bin/bash
# Replace only the host-namespace identity CLI; all other Python stays real.
set -euo pipefail
if [[ $# -ge 4 && $4 == "$NMRNET_FIXTURE_ROOT/host/bootstrap/acquisition/acquisition_tool/identity.py" ]]; then
    [[ $1 == -I && $2 == -S && $3 == -B && $# == 14 ]] || exit 97
    [[ $5 == --endpoint && $6 == "$NMRNET_DOCKER_ENDPOINT_INPUT" && $7 == --image && $9 == --host-uid && ${11} == --host-gid && ${13} == --retired-receipt ]] || exit 97
    printf '%s\0' "$@" >> "$NMRNET_FIXTURE_ROOT/identity-calls"
    printf '\0' >> "$NMRNET_FIXTURE_ROOT/identity-calls"
    (set -o noclobber; printf 'retired\n' > "${14}")
    printf '%s\n' "$NMRNET_FAKE_IDENTITY_OWNER"
    exit 0
fi
exec "$NMRNET_FIXTURE_PYTHON" -B "$@"
