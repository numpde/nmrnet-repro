#!/usr/bin/env bash
# Read an existing result through the deployed GUI; never create Jobs or science.
# GUI precedent df172527: tests/e2e/signed-result-{handoff,content}-proof.mjs.
set -euo pipefail
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repository_root/tools/provider.sh"
source "$repository_root/tools/provider_api_qualification.sh"
mode=${1-}
[[ $# == 1 && ( $mode == test || $mode == live ) ]] || exit 2
required=(BROWSER_IMAGE)
if [[ $mode == live ]]; then required+=(GUI_ORIGIN API_ORIGIN USER_CREDENTIAL RESULT_RECEIPT TLS_CERTIFICATE ADDRESS INTERFACE OUTPUT); fi
for selector in "${required[@]}"; do
    alias=NMRNET_${selector}_INPUT
    [[ -n ${!alias-} ]] || { printf 'GUI result qualification requires explicit %s.\n' "$selector" >&2; exit 2; }
done
[[ $(id -u) != 0 && $(id -g) != 0 && $NMRNET_BROWSER_IMAGE_INPUT =~ ^sha256:[0-9a-f]{64}$ ]] || exit 2
source "$repository_root/tools/git_inputs.sh"
source "$repository_root/tools/git_metadata.sh"
proof_inputs=(Makefile tools/provider_gui_qualification.sh tools/provider.sh tools/provider_api_qualification.sh
    tools/provider_logging.sh tools/docker_client.sh tools/inference_native.sh tools/worker_retirement.sh
    tools/provider_image.sh tools/git_inputs.sh tools/git_metadata.sh tools/container_guard.py
    tools/bound_http_proxy.py tools/provider_proxy.py tests/integration/provider_runtime/gui_proxy.py
    tests/integration/provider_runtime/gui_result.mjs tests/integration/provider_runtime/normative/test_gui_result.mjs
    tests/integration/provider_runtime/adversarial/test_gui_result.mjs)
revision=$(repository_git -C "$repository_root" rev-parse HEAD)
if [[ $mode == live && -n $(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${proof_inputs[@]}") ]]; then
    printf '%s\n' 'Commit GUI qualification inputs before capturing the live read-only proof.' >&2
    exit 2
fi
provider_host_diagnostic=
if [[ $mode == live ]]; then
    provider_host_root=$NMRNET_OUTPUT_INPUT
    for path in "$provider_host_root" "$NMRNET_USER_CREDENTIAL_INPUT" "$NMRNET_RESULT_RECEIPT_INPUT" "$NMRNET_TLS_CERTIFICATE_INPUT"; do
        provider_host_path "$path" || { printf '%s\n' "$provider_host_diagnostic" >&2; exit 2; }
    done
    provider_host_disjoint "$provider_host_root" "$repository_root" || exit 2
    for path in "$NMRNET_USER_CREDENTIAL_INPUT" "$NMRNET_RESULT_RECEIPT_INPUT" "$NMRNET_TLS_CERTIFICATE_INPUT"; do
        provider_host_disjoint "$path" "$provider_host_root" || exit 2
        [[ -f $path && ! -L $path ]] || exit 2
    done
    [[ $(stat -c '%u:%a' -- "$NMRNET_USER_CREDENTIAL_INPUT") =~ ^$(id -u):(400|600)$ ]] || exit 2
    provider_host_ipv4 "$NMRNET_ADDRESS_INPUT" || exit 2
else
    unit_parent=$(umask 077; mktemp -d /tmp/nmrnet-gui-unit.XXXXXXXX)
    provider_host_root=$unit_parent/evidence
fi
provider_host_initialize
finish_gui() {
    local status=$? role
    trap - EXIT
    if [[ $inference_retirement_confirmed == true ]]; then
        provider_host_remove browser || status=2
        # Preserve the route while its original browser ownership is unresolved.
        if [[ ! -v provider_host_ids[browser] ]]; then provider_host_remove proxy || status=2; fi
    fi
    if [[ ${#provider_host_ids[@]} != 0 || $inference_retirement_confirmed != true ]]; then
        printf 'GUI qualification ownership remains unresolved at %q; preserve this host and its receipts.\n' "$provider_host_root" >&2 || :
        while :; do sleep 1 || :; done
    fi
    docker_client_cleanup || status=2
    if [[ -n $provider_host_diagnostic ]]; then printf '%s\n' "$provider_host_diagnostic" >&2 || status=2; fi
    if [[ -n $pending_diagnostic ]]; then printf '%s\n' "$pending_diagnostic" >&2 || status=2; fi
    printf 'GUI qualification evidence: %s\n' "$provider_host_root"
    exit "$status"
}
trap finish_gui EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
proof_root=$provider_host_root/sources
mkdir -m 700 -- "$proof_root"
if [[ $mode == live ]]; then
    copy_git_blobs 'GUI qualification' "$repository_root" "$revision" "$proof_root" "$provider_host_root/inputs" "${proof_inputs[@]}"
else
    # Offline red/green tests capture the authored working bytes, including new
    # files. Live evidence above requires committed bytes and the Git owner.
    for path in "${proof_inputs[@]}"; do
        mkdir -p -- "$(dirname -- "$proof_root/$path")"
        cp -- "$repository_root/$path" "$proof_root/$path"
    done
fi
for helper in provider.sh provider_api_qualification.sh docker_client.sh inference_native.sh worker_retirement.sh; do
    source "$proof_root/tools/$helper"
done
printf '%s\n' "$NMRNET_BROWSER_IMAGE_INPUT" > "$provider_host_root/browser-image-id"
printf '%s\n' "$revision" > "$provider_host_root/revision"
(cd -- "$proof_root"; sha256sum -- "${proof_inputs[@]}") > "$provider_host_root/sources.sha256"
provider_host_grants=(--env HOME=/tmp/home --env NMRNET_IN_CONTAINER=1
    --mount "type=bind,src=$proof_root/tests/integration/provider_runtime,dst=/work/proof,readonly")
if [[ $mode == test ]]; then
    provider_host_argv=(/usr/local/bin/node --test /work/proof/normative/test_gui_result.mjs /work/proof/adversarial/test_gui_result.mjs)
    network=none
else
    print_command=false
    source "$proof_root/tools/provider_image.sh"
    require_provider_image
    browser_grants=("${provider_host_grants[@]}")
    provider_host_grants=(--env NMRNET_IN_CONTAINER=1 --env PYTHONPATH=/fixture
        --mount "type=bind,src=$proof_root/tools,dst=/fixture/tools,readonly"
        --mount "type=bind,src=$proof_root/tests/integration/provider_runtime/gui_proxy.py,dst=/fixture/gui_proxy.py,readonly"
        --mount "type=bind,src=$provider_host_root/routes,dst=/route")
    provider_host_argv=(/opt/provider/environment/bin/python -B /fixture/gui_proxy.py
        --origin "$NMRNET_GUI_ORIGIN_INPUT" --origin "$NMRNET_API_ORIGIN_INPUT"
        --address "$NMRNET_ADDRESS_INPUT" --interface "$NMRNET_INTERFACE_INPUT")
    provider_host_create proxy host "$provider_image_id" provider_host_grants provider_host_argv true
    deadline=$((SECONDS + 10))
    until [[ -f $provider_host_root/routes/port ]]; do
        (( SECONDS < deadline )) || { provider_host_refuse 'The GUI route did not publish its bound listener within ten seconds.'; exit 2; }
        sleep 0.1
    done
    port=$(< "$provider_host_root/routes/port")
    [[ $port =~ ^[1-9][0-9]{0,4}$ && $port -le 65535 ]] || exit 2
    provider_host_grants=("${browser_grants[@]}"
        --mount "type=bind,src=$NMRNET_USER_CREDENTIAL_INPUT,dst=/input/user.json,readonly"
        --mount "type=bind,src=$NMRNET_RESULT_RECEIPT_INPUT,dst=/input/result-read.json,readonly"
        --mount "type=bind,src=$NMRNET_TLS_CERTIFICATE_INPUT,dst=/input/tls.crt,readonly"
        --mount "type=bind,src=$provider_host_root/output,dst=/output")
    provider_host_argv=(/usr/local/bin/node /work/proof/gui_result.mjs
        --gui-origin "$NMRNET_GUI_ORIGIN_INPUT" --api-origin "$NMRNET_API_ORIGIN_INPUT" --proxy-port "$port")
    network=host
fi
provider_host_create browser "$network" "$NMRNET_BROWSER_IMAGE_INPUT" provider_host_grants provider_host_argv true
api_witness_wait browser 110
provider_host_remove browser
printf 'GUI %s checks passed; no Job or model request was submitted.\n' "$mode"
