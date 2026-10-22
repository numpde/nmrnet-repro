#!/usr/bin/env bash
# Opt-in installed-process proof through the actual service launch/retire owner.
# Synthetic data only; no API/model calls, network, credentials or engine grants.
set -euo pipefail
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ $# != 0 || $(id -u) == 0 || $(id -g) == 0 || $repository_root == *','* || $repository_root == *$'\n'* ]]; then
    printf '%s\n' 'Use make test/integration/provider/logging from a nonroot checkout without mount delimiters.' >&2
    exit 2
fi
umask 077
source "$repository_root/tools/provider.sh"
source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
source "$repository_root/tools/worker_retirement.sh"
source "$repository_root/tools/provider_image.sh"
source "$repository_root/tools/git_inputs.sh"
docker_client_directory= provider_host_root= provider_host_diagnostic= pending_diagnostic=
inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic=
provider_host_stop_requested=false print_command=false finished=false
declare -A provider_host_ids=()

finish() {
    local status=$? role
    trap - EXIT
    if [[ $inference_retirement_confirmed == true ]]; then
        for role in "${!provider_host_ids[@]}"; do
            provider_host_remove "$role" || status=2
        done
        docker_client_cleanup || status=2
    else status=2; fi
    if [[ -n $provider_host_diagnostic ]]; then printf '%s\n' "$provider_host_diagnostic" >&2; fi
    if [[ -n $pending_diagnostic ]]; then printf '%s\n' "$pending_diagnostic" >&2; fi
    if [[ $status == 0 && $finished == true ]]; then printf '%s\n' 'Both-stream Docker logging and post-removal retrieval qualification passed.'
    elif [[ $status == 0 ]]; then status=2; fi
    if [[ -n $provider_host_root ]]; then printf 'Private logging evidence: %q\n' "$provider_host_root"; fi
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
docker_client_prepare run || { pending_diagnostic+=$docker_client_diagnostic; exit 2; }
require_provider_image
revision=$(repository_git -C "$repository_root" rev-parse HEAD)
inputs=(Makefile tools/provider_logging_qualification.sh tests/integration/provider_runtime/logging_probe.py
    tools/provider.sh tools/provider_logging.sh tools/provider_logs.sh tools/worker_retirement.sh
    tools/docker_client.sh tools/inference_native.sh tools/provider_image.sh tools/git_inputs.sh tools/git_metadata.sh)
if [[ -n $(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${inputs[@]}") ]]; then
    provider_host_diagnostic='Commit logging qualification inputs before capturing this contained proof.'; exit 2
fi
if [[ -L $repository_root/.work/qualification ]]; then
    provider_host_diagnostic='Logging qualification requires an evidence directory without a symlink.'; exit 2
fi
mkdir -p -- "$repository_root/.work/qualification"
provider_host_root=$(mktemp -d "$repository_root/.work/qualification/logging-XXXXXXXX")
mkdir -m 700 -- "$provider_host_root/sources" "$provider_host_root/components"
copy_git_blobs 'logging qualification' "$repository_root" "$revision" "$provider_host_root/sources" "$provider_host_root/inputs" "${inputs[@]}"
printf '%s\n' "$revision" > "$provider_host_root/revision"
printf '%s\n' "$provider_image_id" > "$provider_host_root/image-id"
(cd -- "$provider_host_root/sources"; sha256sum -- "${inputs[@]}") > "$provider_host_root/sources.sha256"
# Execute the captured shell policy too, rather than only hashing live helpers.
source "$provider_host_root/sources/tools/provider.sh"
for mode in success failure interrupted; do
    output=$provider_host_root/$mode
    mkdir -m 700 -- "$output"
    if [[ $mode == failure ]]; then NMRNET_LOG_LEVEL_INPUT=DEBUG; else NMRNET_LOG_LEVEL_INPUT=INFO; fi
    provider_host_grants=(--mount "type=bind,src=$provider_host_root/sources/tests/integration/provider_runtime/logging_probe.py,dst=/probe.py,readonly"
        --mount "type=bind,src=$output,dst=/proof")
    provider_host_argv=(/opt/provider/environment/bin/python -I -B /probe.py "$mode")
    provider_host_create "$mode" none "$provider_image_id" provider_host_grants provider_host_argv false
    directory=$provider_host_root/components/$mode
    identifier=${provider_host_ids[$mode]}
    deadline=$((SECONDS + 10))
    until [[ -f $output/ready ]]; do
        if (( SECONDS >= deadline )); then provider_host_diagnostic='Logging fixture did not publish readiness within ten seconds.'; exit 2; fi
        sleep 0.05
    done
    if [[ $mode != interrupted ]]; then
        provider_host_command wait "$directory" wait "$identifier"
        if [[ $mode == success ]]; then expected_exit=0; else expected_exit=1; fi
        cmp -s -- "$directory/wait.stdout" <(printf '%s\n' "$expected_exit")
    fi
    provider_host_remove "$mode"
    # Read only the fixed fixture's small log stream into memory. Do not recreate
    # an on-disk export of the Docker logs. Removal receipts remain authoritative.
    observed=false
    for attempt in {1..10}; do
        if journal=$(timeout --signal=TERM --kill-after=1s 10s journalctl --no-pager --lines 100 --output cat "CONTAINER_ID_FULL=$identifier"); then :; else
            provider_host_diagnostic='Cannot read the removed fixture container logs within the bounded journal query.'; exit 2
        fi
        if [[ $mode == failure && $journal == *'PRIVATE_MOLECULE'* ]] \
            || [[ $mode == success && $journal == *'routine-complete'* ]] \
            || [[ $mode == interrupted && $journal == *'PRIVATE_NATIVE_DIAGNOSTIC'* ]]; then observed=true; break; fi
        sleep 0.1
    done
    [[ $observed == true ]]
    [[ $journal == *'routine-start'* && $journal == *'routine-warning'* && $journal == *'dependency-warning'* ]]
    [[ $journal != *'DEPENDENCY_INFO'* ]]
    if [[ $mode == failure ]]; then
        [[ $journal == *'owned-debug'* && $journal == *'Traceback'* && $journal == *'PRIVATE_NATIVE_FAILURE'* ]]
        [[ $journal == *'completed_outcomes='* && $journal == *'PRIVATE_MOLECULE'* ]]
    elif [[ $journal == *'owned-debug'* ]]; then exit 2; fi
    [[ $journal == *'PRIVATE_NATIVE_DIAGNOSTIC'* ]]
    [[ ! -e $directory/diagnostics/stdout && ! -e $directory/diagnostics/stderr ]]
    if operator_view=$(NMRNET_CONTAINER_ID_INPUT=$identifier timeout --signal=TERM --kill-after=1s 10s \
        bash "$provider_host_root/sources/tools/provider_logs.sh"); then :; else
        provider_host_diagnostic='Cannot read the removed fixture through the operator log command.'; exit 2
    fi
    [[ $operator_view == *'routine-start'* && $operator_view == *'PRIVATE_NATIVE_DIAGNOSTIC'* ]]
    printf '%s: both streams retrieved after exact removal; no stream dump files.\n' "$mode"
done
finished=true
