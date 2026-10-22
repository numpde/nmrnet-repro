#!/usr/bin/env bash
set -euo pipefail
# Read-only operator view. A retained worker.cid supplies the ID after removal;
# journal content/presence never attests execution, retirement or publication.
identifier=${NMRNET_CONTAINER_ID_INPUT-}
if [[ ! $identifier =~ ^[0-9a-f]{64}$ ]]; then
    printf '%s\n' 'Use make provider/logs CONTAINER_ID=<full 64-character container ID from worker.cid>.' >&2
    exit 2
fi
journalctl --no-pager --lines 200 --output short-iso "CONTAINER_ID_FULL=$identifier"
