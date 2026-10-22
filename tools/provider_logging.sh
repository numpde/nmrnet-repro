#!/usr/bin/env bash
# Routine stdout is Docker-owned; protected stderr/control files are not logs.
# Journald survives container removal. Its shared disk/age limits belong to the
# host; the nonblocking buffer below bounds delivery, not retained journal size.
# https://docs.docker.com/engine/logging/drivers/journald/
# https://docs.docker.com/engine/logging/configure/#configure-the-delivery-mode-of-log-messages-from-container-to-log-driver

provider_logging_configure() {
    provider_log_level=${NMRNET_LOG_LEVEL_INPUT:-INFO}
    case "$provider_log_level" in
        INFO|DEBUG|WARNING) ;;
        *) printf '%s\n' 'Routine LOG_LEVEL must be INFO, DEBUG or WARNING.' >&2; return 2 ;;
    esac
    provider_logging_options=(--log-driver journald --log-opt mode=non-blocking
        --log-opt max-buffer-size=1m --log-opt 'tag=nmrnet-{{.FullID}}')
    provider_logging_posture='journald|{"max-buffer-size":"1m","mode":"non-blocking","tag":"nmrnet-{{.FullID}}"}'
}
