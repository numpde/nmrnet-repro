#!/usr/bin/env bash
# NMRNet's pool policy inside the shared acquisition scope.
nmrnet_acquire_wheelhouse() {
    local worker_status
    acquisition_worker offline worker_status "$image" offline_grants offline_app || return $?
    case $worker_status in
        0) wheelhouse_warm=true; return 0 ;;
        3) ;;
        *) return "$worker_status" ;;
    esac

    if [[ -z ${interface-} ]]; then select_acquisition_interface || return $?; fi
    acquisition_set_route "$interface" || return $?
    acquisition_worker online worker_status "$image" online_grants online_app || return $?
    if (( worker_status != 0 )); then return "$worker_status"; fi

    acquisition_worker offline worker_status "$image" offline_grants offline_app || return $?
    if (( worker_status == 3 )); then
        printf '%s\n' 'Cannot complete wheelhouse acquisition: required inputs remain missing after the acquisition attempt.' >&2
        return 1
    fi
    return "$worker_status"
}
