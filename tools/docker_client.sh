#!/usr/bin/env bash
# Prepare an operation-owned Docker CLI using the explicitly selected local context.
# Other Docker endpoint/TLS settings are ignored; auth/proxies from a client
# config directory; config proxies can inject container env and build arguments.
# https://docs.docker.com/reference/cli/docker/#environment-variables
# https://docs.docker.com/reference/cli/docker/#change-the-docker-directory
# https://docs.docker.com/engine/cli/proxy/
# Compose also reads ambient lifecycle and path-conversion settings. None is
# an operator grant here; callers supply their recipe/project/env-file literally.
# https://docs.docker.com/compose/how-tos/environment-variables/envvars/#compose_remove_orphans
# https://docs.docker.com/compose/how-tos/environment-variables/envvars/#compose_convert_windows_paths
# Sourcing defines functions only. Callers choose PRINT/run and own traps,
# native-child lifetime and primary status; cleanup does not claim engine-side
# container retirement. Explicit worker --env proxy argv remains authoritative.
# Callers initialize docker_client_directory before installing cleanup traps;
# an ambient shell variable must never grant cleanup authority over a path.
# This repository uses its fixed BuildKit policy, including for future build
# callers with explicit --builder default; ambient DOCKER_BUILDKIT is replaced.
# Preparation returns controlled docker_client_diagnostic text instead of
# writing to public pipes before caller cleanup. Failed config setup retains
# its allocated parent as operator evidence, not as a usable client directory.
# Callers queue the returned text and preserve its status/own retirement fact.

docker_client_prepare() {
    docker_client_diagnostic=
    if [[ $# != 1 ]]; then
        printf -v docker_client_diagnostic '%s\n' 'Docker client preparation requires the explicit print or run mode.'
        return 2
    fi
    local mode=$1 preparation_status config receipt context endpoint context_origin
    context=${NMRNET_DOCKER_CONTEXT_INPUT-}
    context_origin=${NMRNET_DOCKER_CONTEXT_INPUT_ORIGIN-}
    endpoint=${NMRNET_DOCKER_ENDPOINT_INPUT-}
    if [[ -z $endpoint ]]; then
        if [[ $context_origin == 'command line' ]]; then
            if [[ -z $context || $context =~ [[:cntrl:]] ]]; then
                printf -v docker_client_diagnostic '%s\n' 'Cannot select Docker: command-line DOCKER_CONTEXT must name a context.'
                return 2
            fi
        else
            local -a contexts=() endpoints=()
            local candidate candidate_endpoint selection
            if [[ ! -t 0 ]]; then
                printf -v docker_client_diagnostic '%s\n' 'Cannot select Docker: pass DOCKER_CONTEXT=<local-context> on the make command line, or run this command in a terminal to choose a local Unix-socket context.'
                return 2
            fi
            local context_names
            if ! context_names=$(env -u DOCKER_HOST -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
                -u DOCKER_CONTEXT docker context ls --format '{{.Name}}' 2>/dev/null); then
                printf -v docker_client_diagnostic '%s\n' 'Cannot list Docker contexts from the caller Docker configuration.'
                return 2
            fi
            while IFS= read -r candidate; do
                [[ -n $candidate && ! $candidate =~ [[:space:]\|] ]] || continue
                if candidate_endpoint=$(env -u DOCKER_HOST -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
                    -u DOCKER_CONTEXT docker context inspect --format '{{.Endpoints.docker.Host}}' "$candidate" 2>/dev/null) \
                    && [[ $candidate_endpoint == unix:///* && ! $candidate_endpoint =~ [[:space:]\|] ]]; then
                    contexts+=("$candidate")
                    endpoints+=("$candidate_endpoint")
                fi
            done <<< "$context_names"
            if (( ${#contexts[@]} == 0 )); then
                printf -v docker_client_diagnostic '%s\n' 'Cannot select Docker: the caller Docker configuration has no contexts with local Unix-socket endpoints.'
                return 2
            fi
            printf '%s\n' 'Choose a local Docker context:' >&2
            local index
            for index in "${!contexts[@]}"; do printf '  %s) %s\n' "$((index + 1))" "${contexts[index]}" >&2; done
            printf 'Context number: ' >&2
            if ! IFS= read -r selection || [[ ! $selection =~ ^[1-9][0-9]*$ ]] \
                || (( selection > ${#contexts[@]} )); then
                printf -v docker_client_diagnostic '%s\n' 'Cannot select Docker: enter one of the listed local context numbers.'
                return 2
            fi
            index=$((selection - 1))
            context=${contexts[index]}
            endpoint=${endpoints[index]}
        fi
        if [[ -z $endpoint ]]; then
            if ! endpoint=$(env -u DOCKER_HOST -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH \
                -u DOCKER_CONTEXT docker context inspect --format '{{.Endpoints.docker.Host}}' "$context" 2>/dev/null); then
                printf -v docker_client_diagnostic 'Cannot select Docker context %q: inspect the caller Docker configuration.\n' "$context"
                return 2
            fi
        fi
    fi
    if [[ -z $context || $context =~ [[:space:]\|] || $endpoint != unix:///* || $endpoint =~ [[:space:]\|] ]]; then
        printf -v docker_client_diagnostic 'Cannot select Docker context %q: this workflow requires a named local Unix-socket context.\n' "$context"
        return 2
    fi
    # Nested launchers inherit this immutable endpoint snapshot. They must not
    # re-read mutable context metadata during the same Make operation.
    export NMRNET_DOCKER_CONTEXT_INPUT=$context NMRNET_DOCKER_ENDPOINT_INPUT=$endpoint
    case "$mode" in
        print)
            docker_client_directory=
            config=/tmp/nmrnet-docker-client.PRINT/config
            ;;
        run)
            if { docker_client_directory=$(umask 077; mktemp -d /tmp/nmrnet-docker-client.XXXXXXXX); } 2>/dev/null; then :; else
                preparation_status=$?
                # Failed output never admits a path for caller cleanup.
                docker_client_directory=
                printf -v docker_client_diagnostic 'Cannot prepare Docker client configuration: private directory creation under /tmp failed (status %s); no private cause receipt was established.\n' "$preparation_status"
                return "$preparation_status"
            fi
            config=$docker_client_directory/config
            receipt=$docker_client_directory/preparation.log
            # The conditional caller disables errexit: each cap/open failure
            # must stop before mkdir. Quiet outer shell errors cannot block
            # cleanup, and a receipt may be absent or truncated at the cap.
            if { (
                umask 077 || exit "$?"
                ulimit -f 4 || exit "$?"
                exec 3> "$receipt" || exit "$?"
                mkdir -m 700 -- "$config" >&3 2>&1
            ); } 2>/dev/null; then :; else
                preparation_status=$?
                printf -v docker_client_diagnostic 'Cannot prepare Docker client configuration: private config creation failed (status %s); private state was allocated at %q, with capped diagnostic receipt attempted at %q (up to 4096 bytes; it may be absent or incomplete).\n' \
                    "$preparation_status" "$docker_client_directory" "$receipt"
                # No engine was dispatched. As in tools/inference_native.sh,
                # retain the failed operation's private cause; caller runtime
                # cleanup must not erase this evidence directory.
                docker_client_directory=
                return "$preparation_status"
            fi
            ;;
        *)
            printf -v docker_client_diagnostic '%s\n' 'Docker client preparation requires the explicit print or run mode.'
            return 2
            ;;
    esac
    engine=(env)
    local compose_variable
    for compose_variable in "${!COMPOSE_@}"; do
        engine+=(-u "$compose_variable")
    done
    engine+=(-u NMRNET_DOCKER_CONTEXT_INPUT -u NMRNET_DOCKER_CONTEXT_INPUT_ORIGIN -u NMRNET_DOCKER_ENDPOINT_INPUT
        -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH
        -u DOCKER_CONFIG -u DOCKER_AUTH_CONFIG -u DOCKER_CUSTOM_HEADERS -u DOCKER_API_VERSION -u DOCKER_DEFAULT_PLATFORM
        -u BUILDX_BUILDER -u BUILDX_CONFIG -u DOCKER_BUILDKIT
        -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY -u NO_PROXY
        -u http_proxy -u https_proxy -u all_proxy -u no_proxy
        DOCKER_BUILDKIT=1 docker --config "$config" --host "$endpoint")
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
