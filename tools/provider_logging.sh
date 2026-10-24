#!/usr/bin/env bash
# Both process streams are Docker-owned; structured/control files are not logs.
# Finite helpers use the same bounded policy as the Compose runtime. Removing
# a container removes its logs; no host journal or parallel file dump is needed.
# https://docs.docker.com/engine/logging/drivers/json-file/
# https://docs.docker.com/engine/logging/configure/#configure-the-delivery-mode-of-log-messages-from-container-to-log-driver

provider_logging_configure() {
    provider_log_level=${NMRNET_LOG_LEVEL_INPUT:-INFO}
    case "$provider_log_level" in
        INFO|DEBUG|WARNING) ;;
        *) printf '%s\n' 'Routine LOG_LEVEL must be INFO, DEBUG or WARNING.' >&2; return 2 ;;
    esac
    provider_logging_options=(--log-driver json-file --log-opt mode=non-blocking
        --log-opt max-buffer-size=1m --log-opt max-size=10m --log-opt max-file=3)
    provider_logging_posture='json-file|{"max-buffer-size":"1m","max-file":"3","max-size":"10m","mode":"non-blocking"}'
}
