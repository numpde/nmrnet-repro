#!/usr/bin/env bash
# Prepare one operation-owned Docker CLI, independent of ambient client state.
# Docker reads endpoint/TLS settings from environment and auth/proxies from its
# config directory; config proxies can inject container env and build arguments.
# https://docs.docker.com/reference/cli/docker/#environment-variables
# https://docs.docker.com/reference/cli/docker/#change-the-docker-directory
# https://docs.docker.com/engine/cli/proxy/
# Sourcing defines functions only. Callers choose PRINT/run and own traps,
# native-child lifetime and primary status; cleanup does not claim engine-side
# container retirement. Explicit worker --env proxy argv remains authoritative.
# Callers initialize docker_client_directory before installing cleanup traps;
# an ambient shell variable must never grant cleanup authority over a path.
# This repository uses its fixed BuildKit policy, including for future build
# callers with explicit --builder default; ambient DOCKER_BUILDKIT is replaced.

docker_client_prepare() {
    if [[ $# != 1 ]]; then
        printf '%s\n' 'Docker client preparation requires the explicit print or run mode.' >&2
        return 2
    fi
    local mode=$1 preparation_status config
    case "$mode" in
        print)
            docker_client_directory=
            config=/tmp/nmrnet-docker-client.PRINT/config
            ;;
        run)
            if docker_client_directory=$(umask 077; mktemp -d /tmp/nmrnet-docker-client.XXXXXXXX); then :; else
                preparation_status=$?
                printf 'Cannot prepare Docker client configuration: private directory creation failed (status %s).\n' "$preparation_status" >&2
                return "$preparation_status"
            fi
            config=$docker_client_directory/config
            if mkdir -m 700 -- "$config"; then :; else
                preparation_status=$?
                printf 'Cannot prepare Docker client configuration: private config creation failed (status %s).\n' "$preparation_status" >&2
                return "$preparation_status"
            fi
            ;;
        *)
            printf '%s\n' 'Docker client preparation requires the explicit print or run mode.' >&2
            return 2
            ;;
    esac
    engine=(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH
        -u DOCKER_CONFIG -u DOCKER_AUTH_CONFIG -u DOCKER_CUSTOM_HEADERS -u DOCKER_API_VERSION -u DOCKER_DEFAULT_PLATFORM
        -u BUILDX_BUILDER -u BUILDX_CONFIG -u DOCKER_BUILDKIT
        -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY
        -u http_proxy -u https_proxy -u all_proxy -u no_proxy
        DOCKER_BUILDKIT=1 docker --config "$config" --host unix:///var/run/docker.sock)
}

# Remove the prepared directory; failure does not establish what remains there.
docker_client_cleanup() {
    local removal_status
    if [[ ! -v docker_client_directory || -z "$docker_client_directory" ]]; then return 0; fi
    if rm -rf -- "$docker_client_directory"; then
        docker_client_directory=
    else
        removal_status=$?
        printf 'Cannot confirm Docker client configuration removal; attempted path %q (status %s).\n' "$docker_client_directory" "$removal_status" >&2
        return "$removal_status"
    fi
}
