#!/usr/bin/env bash
# User-service operations only. provider.sh still owns containers and retirement.
set -euo pipefail
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
source "$repository_root/tools/provider.sh"
provider_host_diagnostic=
refuse() { printf '%s\n' "$1" >&2; exit 2; }
manager() { timeout --signal=TERM --kill-after=5s 30s systemctl --user "$@"; }
[[ $# == 1 ]] || refuse 'Use make provider/service/{install,start,status,stop,logs,enable,disable} UNIT=<name>.'
operation=$1
case "$operation" in install|start|status|stop|logs|enable|disable) ;; *) refuse 'Unknown provider service operation.' ;; esac
instance=${NMRNET_UNIT_INPUT-}
[[ $instance =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$ ]] || refuse 'UNIT must be an explicit name of at most 48 letters, digits, underscores or hyphens.'
[[ $(id -u) != 0 ]] || refuse 'Manage the provider through its nonroot user service manager.'
[[ ${XDG_CONFIG_HOME:-$HOME/.config} == "$HOME/.config" ]] || refuse 'This user-service template uses the standard ~/.config directory; a different XDG_CONFIG_HOME is unsupported.'
unit=nmrnet-provider@$instance.service
unit_directory=$HOME/.config/systemd/user
profile_directory=$HOME/.config/nmrnet-repro
unit_file=$unit_directory/nmrnet-provider@.service
profile=$profile_directory/$instance.env

private_file() {
    provider_host_path "$1" || refuse "$provider_host_diagnostic"
    [[ -f $1 && ! -L $1 && $(stat -c %u -- "$1") == "$(id -u)" ]] || refuse 'Service profiles must be caller-owned regular files without links.'
    case $(stat -c %a -- "$1") in 400|600) ;; *) refuse 'Service profiles must have mode 0400 or 0600.' ;; esac
    [[ $(stat -c %s -- "$1") -le 65536 ]] || refuse 'Service profiles must fit within 64 KiB.'
}

repository_assignment() {
    local escaped=${repository_root//\\/\\\\}
    escaped=${escaped//\"/\\\"}
    printf 'NMRNET_REPOSITORY_ROOT="%s"' "$escaped"
}

install_service() {
    local selected=${NMRNET_SERVICE_CONFIG_INPUT-} row name staged
    [[ -n $selected ]] || refuse 'Install requires explicit SERVICE_CONFIG pointing to a private copy of config/provider-service.env.example.'
    private_file "$selected"
    while IFS= read -r row || [[ -n $row ]]; do
        [[ -z $row || $row == \#* ]] && continue
        name=${row%%=*}
        case "$name" in
            NMRNET_CONFIG_INPUT|NMRNET_SOURCE_ROOT_INPUT|NMRNET_RUNS_ROOT_INPUT|NMRNET_MAXIMUM_STARTUP_BYTES_INPUT|NMRNET_MAXIMUM_NATIVE_REQUESTS_INPUT|NMRNET_HOST_DRAIN_SECONDS_INPUT|NMRNET_API_ROUTE_INPUT|NMRNET_STORE_ROUTE_INPUT|NMRNET_INTERPRETER_ROUTE_INPUT|NMRNET_LOCAL_NETWORK_INPUT|NMRNET_LOCAL_INTERFACE_INPUT|NMRNET_LOCAL_PEERS_INPUT|NMRNET_MAPPED_INTERFACE_INPUT|NMRNET_MAPPED_ADDRESSES_INPUT|NMRNET_EXTERNAL_INTERFACE_INPUT|NMRNET_RESOLVERS_INPUT|NMRNET_LOG_LEVEL_INPUT) ;;
            *) refuse 'Service profiles accept only the maintained launcher inputs; credentials and arbitrary environment variables do not belong here.' ;;
        esac
        [[ $row == *=* && $row != *$'\r'* && $row != *\\ ]] || refuse 'Each service profile assignment must occupy one complete line.'
    done < "$selected"
    case $(manager show "$unit" --property=ActiveState --value) in
        inactive|failed) ;;
        active|activating|reloading|deactivating) refuse 'Stop this service before changing its installed profile; running work was left untouched.' ;;
        *) refuse 'Cannot establish an inactive service state; no profile was changed.' ;;
    esac
    umask 077
    provider_host_path "$unit_directory" && provider_host_path "$profile_directory" || refuse "$provider_host_diagnostic"
    mkdir -p -- "$unit_directory"
    if [[ ! -e $profile_directory ]]; then mkdir -m 700 -- "$profile_directory"; fi
    [[ ! -L $profile_directory && $(stat -c '%u:%a' -- "$profile_directory") == "$(id -u):700" ]] || refuse 'The service profile directory must be caller-owned, private and not a link.'
    if [[ -e $unit_file || -L $unit_file ]]; then
        [[ -f $unit_file && ! -L $unit_file && $(stat -c '%u:%a' -- "$unit_file") == "$(id -u):600" ]] \
            && IFS= read -r row < "$unit_file" && [[ $row == '# Managed by nmrnet-repro; change the repository template, not this copy.' ]] \
            || refuse 'The existing service template belongs to another owner; it was left unchanged.'
    fi
    staged=$(mktemp "$profile_directory/.install.XXXXXXXX")
    cat -- "$selected" > "$staged"
    printf '\n%s\n' "$(repository_assignment)" >> "$staged"
    mv -T -- "$staged" "$profile"
    install -m 600 -- "$repository_root/deployment/nmrnet-provider@.service" "$unit_file"
    manager daemon-reload
    printf 'Installed %s. Starting now and enabling user-manager startup are separate operations.\n' "$unit"
}

current_installation() {
    local runtime=${XDG_RUNTIME_DIR:?User-service runtime directory is unavailable}/nmrnet-provider-$instance
    [[ -L $runtime/current ]] || return 1
    installation=$(readlink -f -- "$runtime/current")
    provider_host_path "$installation" || refuse "$provider_host_diagnostic"
    [[ -d $installation && $(stat -c '%u:%a' -- "$installation") == "$(id -u):700" ]] || refuse 'The retained installation locator is not a private caller-owned directory.'
}

if [[ $operation == install ]]; then install_service; exit; fi
[[ -f $unit_file && ! -L $unit_file ]] && cmp -s "$unit_file" "$repository_root/deployment/nmrnet-provider@.service" \
    || refuse 'Install this checkout service template before managing the selected instance.'
private_file "$profile"
grep -Fxq -- "$(repository_assignment)" "$profile" \
    || refuse 'This service profile belongs to another checkout; use its owner.'
case "$operation" in
    start) manager start --job-mode=fail "$unit"; printf 'Manager started %s; status distinguishes startup admission from API availability.\n' "$unit" ;;
    stop) manager stop --no-block "$unit"; printf 'Stop requested for %s; use status to observe completed drain. Unresolved work remains owned.\n' "$unit" ;;
    enable) manager enable "$unit"; printf 'Enabled user-manager startup. Starting at boot before login additionally requires administrator-enabled user lingering.\n' ;;
    disable) manager disable "$unit" ;;
    status)
        manager show "$unit" --property=ActiveState,SubState,MainPID,ExecMainStatus,InvocationID,FragmentPath,UnitFileState
        if current_installation; then
            printf 'Retained installation: %s\n' "$installation"
            if [[ -f $installation/components/controller/diagnostics/admitted ]]; then
                printf '%s\n' 'Startup configuration was admitted for this installation; this marker does not establish current API availability.'
            fi
        fi
        ;;
    logs)
        timeout --signal=TERM --kill-after=5s 30s journalctl --user --no-pager --lines 100 --output short-iso --unit "$unit"
        if current_installation && [[ -f $installation/components/controller/worker.cid ]]; then
            NMRNET_CONTAINER_ID_INPUT=$(< "$installation/components/controller/worker.cid") bash "$repository_root/tools/provider_logs.sh"
        fi
        ;;
esac
