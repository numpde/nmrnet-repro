#!/usr/bin/env bash
# Apply one explicit provider image update through the existing lifecycle owners.
set -euo pipefail
umask 077

repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -v NMRNET_RETAIN_PRIOR_INPUT ]] && [[ -n $NMRNET_RETAIN_PRIOR_INPUT && $NMRNET_RETAIN_PRIOR_INPUT != 1 ]]; then
    printf '%s\n' 'Provider update accepts RETAIN_PRIOR=1 to preserve previous images and configuration generations.' >&2
    exit 2
fi
if [[ ! -v NMRNET_DEPLOYMENT_INPUT ]] || [[ ! $NMRNET_DEPLOYMENT_INPUT =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$ ]]; then
    printf '%s\n' 'Provider update requires an explicit named deployment.' >&2
    exit 2
fi
source "$repository_root/tools/image_lifecycle.sh"
image_lifecycle_lock shared

run_phase() {
    local phase=$1 status
    shift
    printf 'Provider update: %s.\n' "$phase"
    if "$@"; then
        return 0
    else
        status=$?
    fi
    printf 'Provider update stopped during %s (status %s). Earlier effects may remain; inspect deployment status and logs before retrying.\n' \
        "$phase" "$status" >&2
    return "$status"
}

run_phase 'build the host-local provider image' bash "$repository_root/tools/container.sh" provider/image/build
run_phase 'drain, install and verify the selected deployment under one lifecycle lock' \
    bash "$repository_root/tools/provider_deployment.sh" update
flock --unlock "$image_lifecycle_fd"
exec {image_lifecycle_fd}<&-

if [[ -v NMRNET_RETAIN_PRIOR_INPUT ]] && [[ $NMRNET_RETAIN_PRIOR_INPUT == 1 ]]; then
    printf '%s\n' 'Provider update retained previous images and configuration generations.'
elif bash "$repository_root/tools/image_retirement.sh"; then
    :
else
    status=$?
    printf 'Deployment is healthy; image retirement is incomplete (status %s). Retry make packages/images/retire.\n' "$status" >&2
fi
