#!/usr/bin/env bash
# Fixed synthetic kernel OOM through the installed serial runner. No Engine,
# credential, network or model grant reaches either contained participant.
set -euo pipefail
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ $# != 0 || $(id -u) == 0 || $(id -g) == 0 || $repository_root == *','* || $repository_root == *$'\n'* ]]; then
    printf '%s\n' 'Use make test/integration/provider/prediction-recovery from a nonroot checkout without mount delimiters.' >&2
    exit 2
fi
umask 077
source "$repository_root/tools/provider.sh"
source "$repository_root/tools/provider_api_qualification.sh"
source "$repository_root/tools/provider_image.sh"
source "$repository_root/tools/git_inputs.sh"
source "$repository_root/tools/git_metadata.sh"
provider_host_root= provider_host_diagnostic= pending_diagnostic= finished=false
inference_retirement_confirmed=true

after_proof() {
    local status=$?
    trap - EXIT
    if [[ $inference_retirement_confirmed == true ]]; then
        # Stop the controller before its native endpoint; the shared owner
        # delivers bounded stopped-container logs before permitted removal.
        provider_host_remove controller || status=2
        if [[ ! -v provider_host_ids[controller] && $inference_retirement_confirmed == true ]]; then
            provider_host_remove runner || status=2
        fi
    fi
    if [[ $inference_retirement_confirmed != true ]]; then
        printf 'The original local client remains unretired. Keep this process and its private evidence at %q.\n' "$provider_host_root" >&2 || :
        while :; do sleep 1 || :; done
    fi
    if (( ${#provider_host_ids[@]} )); then
        printf 'Stopped/uncertain qualification containers and exact receipts remain at %q.\n' "$provider_host_root" >&2 || :
        status=2
    fi
    docker_client_cleanup || status=2
    if [[ -n $provider_host_diagnostic ]]; then printf '%s\n' "$provider_host_diagnostic" >&2 || status=2; fi
    if [[ -n $pending_diagnostic ]]; then printf '%s\n' "$pending_diagnostic" >&2 || status=2; fi
    if [[ $status == 0 && $finished == true ]]; then
        printf '%s\n' 'Synthetic kernel OOM recovery passed: one replacement in the same runner, exact frozen inputs and confirmed child retirement.'
    elif [[ $status == 0 ]]; then status=2; fi
    printf 'Private prediction-recovery evidence: %q\n' "$provider_host_root" || status=2
    exit "$status"
}

revision=$(repository_git -C "$repository_root" rev-parse HEAD)
inputs=(Makefile tools/prediction_recovery_qualification.sh tests/integration/provider_worker/prediction_recovery_probe.py
    nmrnet_inference/worker.py
    tools/provider.sh tools/provider_api_qualification.sh tools/provider_logging.sh tools/worker_retirement.sh tools/inference_native.sh
    tools/docker_client.sh tools/provider_image.sh tools/git_inputs.sh tools/git_metadata.sh)
if [[ -n $(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${inputs[@]}") ]]; then
    printf '%s\n' 'Commit prediction-recovery qualification inputs before running their frozen contained proof.' >&2
    exit 2
fi
[[ ! -L $repository_root/.work/qualification ]] || exit 2
mkdir -p -m 700 -- "$repository_root/.work/qualification"
scratch=$(mktemp -d "$repository_root/.work/qualification/prediction-recovery-XXXXXXXX")
provider_host_root=$scratch/owner
provider_host_initialize
trap after_proof EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
print_command=false
require_provider_image
mkdir -m 700 -- "$provider_host_root/sources" "$provider_host_root/observations" \
    "$provider_host_root/native" "$provider_host_root/native/prediction"
: > "$provider_host_root/native/prediction/.owner-session.lock"
copy_git_blobs 'prediction recovery qualification' "$repository_root" "$revision" "$provider_host_root/sources" "$provider_host_root/inputs" "${inputs[@]}"
printf '%s\n' "$revision" > "$provider_host_root/revision"
printf '%s\n' "$provider_image_id" > "$provider_host_root/image-id"
(cd -- "$provider_host_root/sources"; sha256sum -- "${inputs[@]}") > "$provider_host_root/sources.sha256"
probe=$provider_host_root/sources/tests/integration/provider_worker/prediction_recovery_probe.py
python=/opt/provider/environment/bin/python
provider_host_grants=(--mount "type=bind,src=$probe,dst=/proof/probe.py,readonly"
    --mount "type=bind,src=$provider_host_root/sources/nmrnet_inference/worker.py,dst=/opt/provider/environment/lib/python3.12/site-packages/nmrnet_inference/worker.py,readonly"
    --mount "type=bind,src=$provider_host_root/observations,dst=/proof/observations"
    --mount "type=bind,src=$provider_host_root/native/prediction,dst=/native/prediction"
    --tmpfs "/run:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid=$(id -u),gid=$(id -g)"
    --tmpfs "/scratch:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid=$(id -u),gid=$(id -g)")
provider_host_argv=("$python" -I -B -m provider_core.runner --role prediction
    --socket /native/prediction/runner.sock --scratch /scratch --status-directory /run
    --operation-seconds 25 --startup-seconds 5 --terminate-seconds 1 --kill-seconds 2 --cleanup-seconds 2
    -- "$python" -I -B /proof/probe.py worker)
provider_host_create runner none "$provider_image_id" provider_host_grants provider_host_argv true
deadline=$((SECONDS + 15))
until [[ -S $provider_host_root/native/prediction/runner.sock ]]; do
    (( SECONDS < deadline )) || { provider_host_refuse 'The synthetic prediction runner did not open its endpoint within fifteen seconds.'; exit 2; }
    sleep 0.05
done
provider_host_grants=(--mount "type=bind,src=$probe,dst=/proof/probe.py,readonly"
    --mount "type=bind,src=$provider_host_root/observations,dst=/proof/observations,readonly"
    --mount "type=bind,src=$provider_host_root/native/prediction,dst=/native/prediction,readonly"
    --mount "type=bind,src=$provider_host_root/output,dst=/output")
provider_host_argv=("$python" -I -B /proof/probe.py controller)
provider_host_create controller none "$provider_image_id" provider_host_grants provider_host_argv true
api_witness_wait controller 90
provider_host_command runner-state "$provider_host_root" inspect --type container --format \
    '{{.Id}}|{{.RestartCount}}|{{.State.Running}}|{{.State.Restarting}}' "${provider_host_ids[runner]}"
cmp -s "$provider_host_root/runner-state.stdout" <(printf '%s|0|true|false\n' "${provider_host_ids[runner]}")
[[ -f $provider_host_root/output/summary.json && ! -L $provider_host_root/output/summary.json \
    && $(stat -c '%s' -- "$provider_host_root/output/summary.json") -lt 65536 ]]
finished=true
