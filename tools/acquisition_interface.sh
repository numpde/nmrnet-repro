#!/usr/bin/env bash
# Interface inventory and controlling-terminal choices follow Magnet:
# https://github.com/numpde/magnet-deploy/blob/0619d9ffd92a4326758b442c532aa09496161098/scripts/provider-deployment.sh#L905-L923
# https://github.com/numpde/magnet-deploy/blob/0619d9ffd92a4326758b442c532aa09496161098/scripts/operator-input.sh#L19-L92
# Retain this repository's available-interface admission, without a routing
# default or connectivity guess. The proxy binds IO to the selected device.

acquisition_interface_available() {
    [[ $1 =~ ^[A-Za-z0-9_.:-]{1,15}$ && $1 != . && $1 != .. && -d /sys/class/net/$1 ]]
}

select_acquisition_interface() {
    local tty_fd path name answer index
    local -a choices=()
    if [[ -n ${NMRNET_WLAN_INTERFACE_INPUT-} || ${NMRNET_WLAN_INTERFACE_INPUT_ORIGIN-} == 'command line' ]]; then
        interface=${NMRNET_WLAN_INTERFACE_INPUT-}
    else
        for path in /sys/class/net/*; do
            name=${path##*/}
            if acquisition_interface_available "$name"; then choices+=("$name"); fi
        done
        if ! { exec {tty_fd}<>/dev/tty; } 2>/dev/null; then
            printf '%s\n' 'Cannot acquire artifacts or packages without an interactive terminal: supply WLAN_INTERFACE=<interface> on the make command line.' >&2
            for name in "${choices[@]}"; do printf '  WLAN_INTERFACE=%s\n' "$name" >&2; done
            return 2
        fi
        if [[ ${#choices[@]} == 0 ]]; then
            exec {tty_fd}>&-
            printf '%s\n' 'Cannot select WLAN_INTERFACE: no available network interfaces were found.' >&2
            return 2
        fi
        printf 'Select WLAN_INTERFACE (no default):\n' >&"$tty_fd"
        for index in "${!choices[@]}"; do
            printf '  %d) %s\n' "$((index + 1))" "${choices[index]}" >&"$tty_fd"
        done
        printf 'Choice [1-%d]: ' "${#choices[@]}" >&"$tty_fd"
        if ! IFS= read -r answer <&"$tty_fd"; then
            exec {tty_fd}>&-
            printf '%s\n' 'WLAN_INTERFACE input ended before a choice was read.' >&2
            return 2
        fi
        exec {tty_fd}>&-
        interface=
        # Compare displayed numbers as strings; user input never enters shell
        # arithmetic, array subscripts or command evaluation.
        for index in "${!choices[@]}"; do
            if [[ $answer == "$((index + 1))" ]]; then interface=${choices[index]}; break; fi
        done
        if [[ -z $interface ]]; then
            printf '%s\n' 'WLAN_INTERFACE selection must be one listed number; there is no default.' >&2
            return 2
        fi
    fi
    if ! acquisition_interface_available "$interface"; then
        printf 'Cannot acquire artifacts or packages: WLAN_INTERFACE=%q is not an available network interface.\n' "$interface" >&2
        return 2
    fi
    # Wrappers resolve before image inspection. Later check/run children receive
    # this same value and validate it again without prompting or choosing a route.
    export NMRNET_WLAN_INTERFACE_INPUT=$interface
}
