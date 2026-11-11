#!/usr/bin/env bash
# Translate one NMRNet acquisition worker into the shared session contract.
set -euo pipefail
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly bootstrap_image='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
print_command=false
note=
receipt=
while [[ $# -gt 0 ]]; do
    case $1 in
        --print) print_command=true; shift ;;
        --success-note)
            if (( $# < 2 )); then
                printf '%s\n' 'Acquisition --success-note requires a value.' >&2
                exit 2
            fi
            note=$2; shift 2 ;;
        --retired-receipt)
            if (( $# < 2 )); then
                printf '%s\n' 'Acquisition --retired-receipt requires a value.' >&2
                exit 2
            fi
            receipt=$2; shift 2 ;;
        --image)
            if (( $# < 2 )); then
                printf '%s\n' 'Acquisition requires an explicit worker image.' >&2
                exit 2
            fi
            image=$2; shift 2; break ;;
        *) printf '%s\n' 'Acquisition requires an explicit worker image.' >&2; exit 2 ;;
    esac
done
(( $# > 0 )) || exit 2
[[ -n $image && $1 == -- ]] || exit 2
shift
grants=()
while [[ $# -gt 0 && $1 != -- ]]; do grants+=("$1"); shift; done
(( $# > 0 )) || exit 2
[[ $1 == -- ]] || exit 2
shift
app=("$@")
[[ ${#app[@]} -gt 0 ]] || exit 2
source "$repository_root/tools/acquisition_interface.sh"
select_acquisition_interface
source "$repository_root/tools/docker_client.sh"
docker_client_prepare print || { printf '%s' "$docker_client_diagnostic" >&2; exit 2; }
if "$print_command"; then
    printf 'Docker context: %s (%s), acquisition route: %s\n' "$NMRNET_DOCKER_CONTEXT_INPUT" "$NMRNET_DOCKER_ENDPOINT_INPUT" "$interface"
    printf 'Online worker: '; printf '%q ' "$image" "${grants[@]}" -- "${app[@]}"; printf '\n'
    exit
fi
python3 -B "$repository_root/host/acquisition_bootstrap.py" --root "$repository_root"
source "$repository_root/host/bootstrap/acquisition/acquisition_tool/owner.sh"
run_worker() {
    local worker_status
    acquisition_set_route "$interface" || return $?
    acquisition_worker online worker_status "$image" grants app || return $?
    return "$worker_status"
}
scope=(--endpoint "$NMRNET_DOCKER_ENDPOINT_INPUT" --bootstrap-image "$bootstrap_image")
if [[ -n $receipt ]]; then scope+=(--retired-receipt "$receipt"); fi
if acquisition_scope "${scope[@]}" -- run_worker; then :; else exit "$?"; fi
if [[ -n $note ]]; then
    source "$repository_root/tools/inference_native.sh"
    source "$repository_root/tools/operator_guidance.sh"
    inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic=
    report_next_actions "$note"
fi
