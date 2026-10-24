#!/usr/bin/env bash
# Finite stopped-store inspection or exact API replay; never launches science.
set -euo pipefail
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ $# != 1 || ( $1 != inspect && $1 != run ) ]]; then
    printf '%s\n' 'Use make provider/recovery/inspect or provider/recovery/run with explicit startup inputs.' >&2
    exit 2
fi
source "$repository_root/tools/provider.sh"
provider_host_diagnostic=
trap 'status=$?; if [[ -n $provider_host_diagnostic ]]; then printf "%s\n" "$provider_host_diagnostic" >&2 || status=2; fi; exit "$status"' EXIT
provider_host_recovery_main "$1"
exit "$?"
