#!/usr/bin/env bash
# Host orchestration uses the existing component admission/retirement owners.
# Only public configuration and the selected deployment's private input files
# enter the offline helper. Its generated profile is installed after retirement.
set -euo pipefail
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repository_root/tools/provider.sh"
provider_host_diagnostic=
trap 'status=$?; printf "%s\n" "$provider_host_diagnostic" >&2; exit "$status"' EXIT
[[ $# == 1 && ( $1 == install || $1 == check ) ]] || { printf '%s\n' 'Use make provider/deployment/install or test/unit/provider/deployment.' >&2; exit 2; }
operation=$1
print_command=false
[[ $(id -u) != 0 && $(id -g) != 0 ]] || { printf '%s\n' 'Manage deployment inputs as their nonroot owner.' >&2; exit 2; }
if [[ $operation == install ]]; then
    deployment=${NMRNET_DEPLOYMENT_INPUT-}
    instance=${NMRNET_UNIT_INPUT-}
    [[ $deployment =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$ && $instance =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$ ]] \
        || { printf '%s\n' 'Supply explicit DEPLOYMENT and UNIT names, each up to 48 letters, digits, underscores or hyphens.' >&2; exit 2; }
    case $(timeout --signal=TERM --kill-after=5s 30s systemctl --user show "nmrnet-provider@$instance.service" --property=ActiveState --value) in
        inactive|failed) ;;
        *) printf '%s\n' 'Deployment installation requires a stopped service (inactive or failed); existing work was left untouched.' >&2; exit 2 ;;
    esac
    config=$repository_root/config/deployments/$deployment
    secrets=$repository_root/secrets/deployments/$deployment
    for path in "$config" "$secrets"; do
        provider_host_path "$path"
        [[ -d $path && ! -L $path && $(stat -c '%u:%a' -- "$path") == "$(id -u):700" ]] \
            || { printf '%s\n' 'Deployment input directories must be caller-owned mode 0700 without symlinks.' >&2; exit 2; }
    done
    if [[ ! -e $secrets/bundles ]]; then mkdir -m 700 -- "$secrets/bundles"; fi
    provider_host_path "$secrets/bundles"
    [[ $(stat -c '%u:%a' -- "$secrets/bundles") == "$(id -u):700" ]]
fi
umask 077
mkdir -p -- "$repository_root/.work/provider-deployments"
provider_host_path "$repository_root/.work/provider-deployments"
provider_host_root=$(mktemp -d "$repository_root/.work/provider-deployments/operation.XXXXXXXX")
provider_host_drain_seconds=30
provider_host_initialize
source "$repository_root/tools/provider_image.sh"
require_provider_image
grants=(--mount "type=bind,src=$repository_root/tools/provider_deployment.py,dst=/source/tools/provider_deployment.py,readonly")
if [[ $operation == check ]]; then
    grants+=(--mount "type=bind,src=$repository_root/tests/integration/provider_core,dst=/source/tests/integration/provider_core,readonly"
        --mount "type=bind,src=$repository_root/packages/provider-core/tests/fakes,dst=/source/tests/fakes,readonly")
    command=(/opt/provider/environment/bin/python -I -B -c '
import sys, unittest
sys.path.insert(0, "/source")
suite = unittest.defaultTestLoader.loadTestsFromNames([
    "tests.integration.provider_core.normative.test_provider_deployment",
    "tests.integration.provider_core.adversarial.test_provider_deployment"])
raise SystemExit(not unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful())')
else
    bundle=$(mktemp -d "$secrets/bundles/generation-XXXXXXXX")
    grants+=(--mount "type=bind,src=$config,dst=$config,readonly"
        --mount "type=bind,src=$secrets,dst=$secrets,readonly"
        --mount "type=bind,src=$bundle,dst=$bundle"
        --mount "type=bind,src=$provider_host_root/output,dst=/output")
    command=(/opt/provider/environment/bin/python -I -B /source/tools/provider_deployment.py
        --definition "$config/provider.toml" --settings "$config/deployment.toml"
        --profile "$config/service.env" --secrets "$secrets" --bundle "$bundle" --output /output)
fi
provider_host_create capture none "$provider_image_id" grants command false
provider_host_command wait "$provider_host_root/components/capture" wait "${provider_host_ids[capture]}"
cmp -s -- "$provider_host_root/components/capture/wait.stdout" <(printf '0\n') \
    || { printf 'Deployment helper failed; read its bounded Docker logs using worker.cid under %s.\n' "$provider_host_root/components/capture" >&2; exit 2; }
provider_host_remove capture
if [[ $operation == install ]]; then
    NMRNET_SERVICE_CONFIG_INPUT=$provider_host_root/output/service.env bash "$repository_root/tools/provider_service.sh" install
    printf 'Installed deployment %s from bundle %s. The service was not started.\n' "$deployment" "$bundle"
else
    printf 'Focused deployment checks passed. Receipts: %s\n' "$provider_host_root"
fi
