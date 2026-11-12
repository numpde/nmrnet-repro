#!/usr/bin/env bash
# Local image preflight and standalone owner mapping for acquisition callers.
set -euo pipefail
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
print_command=false
if [[ $# -gt 0 && $1 == --print ]]; then print_command=true; shift; fi
pool_owner=
if [[ $# -gt 0 && $1 == --pool-owner ]]; then
    [[ $# -ge 2 && $2 =~ ^([0-9]+):([0-9]+)$ ]] || { printf '%s\n' 'Supply --pool-owner UID:GID.' >&2; exit 2; }
    pool_owner=$2
    shift 2
fi
operation=
if [[ $# -gt 0 ]]; then
    operation=$1
    shift
fi
case $operation in
    check-interface)
        source "$repository_root/tools/acquisition_interface.sh"
        select_acquisition_interface
        exit ;;
    map-owner)
        [[ -n $pool_owner && $# == 1 ]] || { printf '%s\n' 'Map an owner with --pool-owner UID:GID map-owner IMAGE.' >&2; exit 2; }
        source "$repository_root/tools/docker_client.sh"
        docker_client_prepare print || { printf '%s' "$docker_client_diagnostic" >&2; exit 2; }
        if "$print_command"; then printf '%s\n' 'Map the host owner through a verified offline engine probe.'; exit; fi
        python3 -I -S -B "$repository_root/host/bootstrap/acquisition/acquisition_tool/source_update.py" verify --source-dir "$repository_root/host/bootstrap/acquisition"
        source "$repository_root/host/bootstrap/acquisition/acquisition_tool/owner.sh"
        NMR_SELECTED_DOCKER_ENDPOINT=$NMRNET_DOCKER_ENDPOINT_INPUT
        host_uid=${pool_owner%:*} host_gid=${pool_owner#*:}
        acquisition_map_owner mapped_uid mapped_gid "$1" "$host_uid" "$host_gid"
        printf '%s:%s\n' "$mapped_uid" "$mapped_gid"
        exit ;;
    require-images)
        [[ $# -gt 0 ]] || exit 0 ;;
    *) printf '%s\n' 'Usage: tools/docker.sh [--print] {check-interface|require-images IMAGE...|--pool-owner UID:GID map-owner IMAGE}' >&2; exit 2 ;;
esac
source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
docker_client_directory=
inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic=
caller_cancellation=0
pending_diagnostic=
scratch=
retain_scratch=false
cleanup() {
    local status=$? cleanup_status=0 cancelled=0
    trap - EXIT
    trap 'cancelled=129' HUP
    trap 'cancelled=130' INT
    trap 'cancelled=143' TERM
    if "$inference_retirement_confirmed"; then
        if docker_client_cleanup 2>/dev/null; then :; else
            cleanup_status=$?
            printf -v pending_diagnostic '%sCannot confirm Docker client configuration removal; attempted path %q (status %s).\n' "$pending_diagnostic" "$docker_client_directory" "$cleanup_status"
        fi
        if [[ -n $scratch && $retain_scratch == false ]]; then
            if rm -rf -- "$scratch" 2>/dev/null; then scratch=; else
                cleanup_status=$?
                printf -v pending_diagnostic '%sCannot remove image preflight receipts at %q (status %s).\n' "$pending_diagnostic" "$scratch" "$cleanup_status"
            fi
        fi
    elif [[ -n $docker_client_directory ]]; then
        printf -v pending_diagnostic '%sCannot confirm image preflight retirement; private Docker state retained at %q.\n' "$pending_diagnostic" "$docker_client_directory"
    fi
    if [[ -n $scratch && $retain_scratch == true && $status != 130 && $status != 143 ]]; then
        printf -v pending_diagnostic '%sAcquisition private receipts retained at %q.\n' "$pending_diagnostic" "$scratch"
    fi
    if [[ $cancelled != 0 ]]; then status=$cancelled
    elif [[ $caller_cancellation != 0 ]]; then status=$caller_cancellation
    elif [[ $inference_cancellation_status != 0 ]]; then status=$inference_cancellation_status
    elif [[ $status == 0 && $cleanup_status != 0 ]]; then status=$cleanup_status; fi
    if [[ -n $inference_diagnostic || -n $pending_diagnostic ]]; then
        if "$inference_retirement_confirmed"; then
            if inference_native 'image preflight reporting' untimed stdio bash -eu -c 'printf "%s%s" "$1" "$2" >&2' bash "$inference_diagnostic" "$pending_diagnostic"; then :; else
                report_status=$?
                if [[ $inference_cancellation_status != 0 ]]; then status=$inference_cancellation_status
                elif [[ $status == 0 ]]; then status=$report_status; fi
            fi
        else
            printf '%s%s' "$inference_diagnostic" "$pending_diagnostic" >&2
        fi
    fi
    if [[ $cancelled != 0 ]]; then status=$cancelled; fi
    exit "$status"
}
trap cleanup EXIT
trap 'caller_cancellation=129; exit 129' HUP
trap 'caller_cancellation=130; exit 130' INT
trap 'caller_cancellation=143; exit 143' TERM
if "$print_command"; then docker_client_prepare print; else docker_client_prepare run; fi || { pending_diagnostic+=$docker_client_diagnostic; exit 2; }
if ! "$print_command"; then scratch=$(umask 077; mktemp -d /tmp/nmrnet-preflight.XXXXXXXX) || exit 2; fi
index=0
for image in "$@"; do
    if "$print_command"; then printf '%q ' "${engine[@]}" image inspect "$image"; printf '\n'; continue; fi
    index=$((index + 1))
    diagnostic=$scratch/preload-$index.stderr
    if inference_native 'acquisition image preflight' 30 stdio bash -eu -c '
        diagnostic=$1; shift; umask 077; ulimit -f 4
        exec "$@" >/dev/null 2> "$diagnostic"
    ' bash "$diagnostic" "${engine[@]}" image inspect "$image"; then
        if [[ ! -f $diagnostic || -L $diagnostic ]] || [[ $(stat -c '%s' -- "$diagnostic") -ge 4096 ]]; then
            retain_scratch=true
            pending_diagnostic='Cannot inspect acquisition image: its diagnostic receipt is incomplete.'$'\n'
            exit 2
        fi
    else
        status=$?
        retain_scratch=true
        if [[ $status == 130 || $status == 143 ]]; then exit "$status"; fi
        if [[ $status == 124 || $status == 137 ]]; then
            printf -v pending_diagnostic 'Cannot start acquisition: pinned-image inspection was interrupted (status %s). No acquisition was started.\n' "$status"
            exit "$status"
        fi
        printf -v pending_diagnostic 'Cannot verify pinned image availability in the local Docker engine (inspection status %s): %s. No acquisition was started.\nInspection failure does not show whether the image is absent or the Docker daemon is unavailable; check both.\nIf the image is missing, preload it through an independently interface-bound process.\n' "$status" "$image"
        exit 2
    fi
done
