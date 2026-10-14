#!/usr/bin/env bash
# Interface-bound HTTP transport for the repository's acquisition clients.
# Transport precedent: explicit interface, loopback proxy, host-network clients.
# https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/tools/docker.sh
# Containerizing the proxy and owning both container lifecycles are local changes.
set -euo pipefail

readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly proxy_image='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
engine=(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH
    docker --host unix:///var/run/docker.sock)
print_command=false
if [[ "${1:-}" == --print ]]; then print_command=true; shift; fi

require_interface() {
    interface="${NMRNET_WLAN_INTERFACE_INPUT:-}"
    if [[ -z "$interface" ]]; then
        printf '%s\n' 'Cannot acquire artifacts or packages: supply WLAN_INTERFACE=<interface> on the make command line.' >&2
        exit 2
    fi
    if [[ ! "$interface" =~ ^[A-Za-z0-9_.:-]{1,15}$ || "$interface" == . || "$interface" == .. || ! -d "/sys/class/net/$interface" ]]; then
        printf 'Cannot acquire artifacts or packages: WLAN_INTERFACE=%q is not an available network interface.\n' "$interface" >&2
        exit 2
    fi
}

require_images() {
    local image
    for image in "$@"; do
        if ! "${engine[@]}" image inspect "$image" >/dev/null; then
            printf 'Cannot start acquisition: the pinned image must be preloaded in the local Docker engine: %s\n' "$image" >&2
            printf '%s\n' 'This launcher cannot bind daemon pulls to WLAN_INTERFACE; provision the image through an independently interface-bound process.' >&2
            exit 2
        fi
    done
}

case "${1:-}" in
    check-interface) require_interface; exit ;;
    require-images) shift; require_images "$@"; exit ;;
    run) ;;
    *) printf '%s\n' 'Usage: tools/docker.sh [--print] {check-interface|require-images IMAGE...|run DOCKER_RUN_ARGS...}' >&2; exit 2 ;;
esac
require_interface
shift
readonly caller_uid="$(id -u)"
readonly caller_gid="$(id -g)"
if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
    printf '%s\n' 'Acquisition requires a nonzero host UID and GID.' >&2
    exit 2
fi

scratch='/tmp/nmrnet-proxy.PRINT'
proxy_id=''
worker_pid=''
cleanup() {
    local status=$?
    local cleanup_failed=false
    trap - EXIT HUP INT TERM
    if [[ -s "$scratch/worker.cid" ]]; then
        if ! "${engine[@]}" rm --force "$(<"$scratch/worker.cid")" >/dev/null; then
            printf 'Cannot confirm acquisition worker removal; container ID is recorded in %s/worker.cid.\n' "$scratch" >&2
            cleanup_failed=true
            if [[ -n "$worker_pid" ]]; then kill "$worker_pid" 2>/dev/null || true; fi
        fi
    fi
    if [[ -n "$worker_pid" ]]; then wait "$worker_pid" 2>/dev/null || true; fi
    if [[ -s "$scratch/proxy.cid" ]]; then
        if ! "${engine[@]}" rm --force "$(<"$scratch/proxy.cid")" >/dev/null; then
            printf 'Cannot confirm proxy removal; container ID is recorded in %s/proxy.cid.\n' "$scratch" >&2
            cleanup_failed=true
        fi
    fi
    if "$cleanup_failed"; then
        if [[ "$status" == 0 ]]; then status=1; fi
    elif ! rm -rf -- "$scratch"; then
        printf 'Cannot remove acquisition scratch directory: %s\n' "$scratch" >&2
        if [[ "$status" == 0 ]]; then status=1; fi
    fi
    exit "$status"
}
if ! "$print_command"; then
    require_images "$proxy_image"
    scratch="$(mktemp -d /tmp/nmrnet-proxy.XXXXXXXX)"
    trap cleanup EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    mkdir -- "$scratch/ready"
fi
# The proxy shares the tools namespace with its guard; neither source is writable.
proxy=("${engine[@]}" create --cidfile "$scratch/proxy.cid" --pull never --network host --read-only
    --env NMRNET_IN_CONTAINER=1 --workdir /workspace
    --user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 1 --memory 128m --memory-swap 128m --pids-limit 64
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=16m
    --mount "type=bind,src=$repository_root/tools/bound_http_proxy.py,dst=/workspace/tools/bound_http_proxy.py,readonly"
    --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
    --mount "type=bind,src=$scratch/ready,dst=/ready"
    "$proxy_image" python3 -B -m tools.bound_http_proxy --interface "$interface" --ready-file /ready/port)
if "$print_command"; then
    printf '%q ' "${proxy[@]}"; printf '\n'
    printf '%q ' "${engine[@]}" start PROXY_ID; printf '\n'
    proxy_port=PORT
else
    proxy_id="$("${proxy[@]}")"
    "${engine[@]}" start "$proxy_id" >/dev/null
    for _ in {1..100}; do
        [[ -s "$scratch/ready/port" ]] && break
        if [[ "$("${engine[@]}" inspect --format '{{.State.Running}}' "$proxy_id")" != true ]]; then
            "${engine[@]}" logs "$proxy_id" >&2 || true
            printf '%s\n' 'Cannot start acquisition: the interface-bound proxy stopped before becoming ready.' >&2
            exit 2
        fi
        sleep 0.05
    done
    if [[ ! -s "$scratch/ready/port" ]]; then
        printf '%s\n' 'Cannot start acquisition: the interface-bound proxy did not become ready.' >&2
        exit 2
    fi
    read -r proxy_port < "$scratch/ready/port"
    if [[ ! "$proxy_port" =~ ^[0-9]+$ || "$proxy_port" -lt 1 || "$proxy_port" -gt 65535 ]]; then
        printf '%s\n' 'Cannot start acquisition: the proxy readiness file contains an invalid port.' >&2
        exit 2
    fi
fi
proxy_url="http://127.0.0.1:$proxy_port"
worker=("${engine[@]}" create --cidfile "$scratch/worker.cid" --network host
    --env "HTTP_PROXY=$proxy_url" --env "HTTPS_PROXY=$proxy_url"
    --env "http_proxy=$proxy_url" --env "https_proxy=$proxy_url"
    --env NO_PROXY= --env no_proxy= --env ALL_PROXY= --env all_proxy= "$@")
if "$print_command"; then
    printf '%q ' "${worker[@]}"; printf '\n'
    printf '%q ' "${engine[@]}" start --attach WORKER_ID; printf '\n'
else
    worker_id="$("${worker[@]}")"
    "${engine[@]}" start --attach "$worker_id" &
    worker_pid=$!
    status=0
    wait "$worker_pid" || status=$?
    worker_pid=''
    exit "$status"
fi
