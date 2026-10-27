#!/usr/bin/env bash
# One canonical checkout owns NMRNet images on the fixed Docker engine.
# Readers retain this directory lease through image/config publication; retirement
# takes it exclusively. Directory inodes need no separate lock or inventory file.

image_lifecycle_lock() {
    local mode=$1 status
    exec {image_lifecycle_fd}< "$repository_root"
    case "$mode" in
        shared)
            if flock --shared --wait 60 "$image_lifecycle_fd"; then return; else status=$?; fi
            ;;
        exclusive)
            if flock --exclusive --nonblock "$image_lifecycle_fd"; then return; else status=$?; fi
            ;;
        *) printf 'Unknown image-lifecycle lock mode: %s\n' "$mode" >&2; return 2 ;;
    esac
    exec {image_lifecycle_fd}<&-
    [[ $status == 1 ]] || return "$status"
    printf '%s\n' 'NMRNet image lifecycle is busy; retry after the current build, deployment or retirement finishes.' >&2
    return 75
}
