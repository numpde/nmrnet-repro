#!/usr/bin/env bash
# Free-text operator selection follows Magnet's controlling-terminal owner:
# https://github.com/numpde/magnet-deploy/blob/0619d9ffd92a4326758b442c532aa09496161098/scripts/operator-input.sh#L19-L66
# Callers own creation and mounts. This resolves data only, without a default.

select_wheelhouse() {
    local tty_fd directory
    if [[ ${NMRNET_WHEELHOUSE_INPUT_ORIGIN-} == 'command line' ]]; then
        wheelhouse_directory=${NMRNET_WHEELHOUSE_INPUT-}
    else
        if ! { exec {tty_fd}<>/dev/tty; } 2>/dev/null; then
            printf '%s\n' 'WHEELHOUSE must be set on the make command line when no interactive terminal is available: WHEELHOUSE=/absolute/cache/root' >&2
            return 2
        fi
        printf 'WHEELHOUSE (absolute shared cache root): ' >&"$tty_fd"
        if ! IFS= read -r wheelhouse_directory <&"$tty_fd"; then
            exec {tty_fd}>&-
            printf '%s\n' 'WHEELHOUSE input ended before a path was read.' >&2
            return 2
        fi
        exec {tty_fd}>&-
    fi
    if [[ $wheelhouse_directory != /* || $wheelhouse_directory == / || $wheelhouse_directory == *','* \
        || $wheelhouse_directory == *$'\n'* || $wheelhouse_directory == *$'\r'* ]] \
        || [[ $(realpath -m -- "$wheelhouse_directory") != "$wheelhouse_directory" ]]; then
        printf 'Cannot use WHEELHOUSE=%q: select a canonical absolute directory without symlinks, commas or line breaks; no default is selected.\n' "$wheelhouse_directory" >&2
        return 2
    fi
    directory=$wheelhouse_directory
    while [[ -n $directory ]]; do
        if [[ -L $directory || ( -e $directory && ! -d $directory ) ]]; then
            printf 'Cannot use WHEELHOUSE: expected a directory without a symlink at %q.\n' "$directory" >&2
            return 2
        fi
        directory=${directory%/*}
    done
}

require_wheelhouse_set() {
    local profile=$1 directory="$wheelhouse_directory/$1"
    if [[ ! -d $directory || -L $directory ]]; then
        printf 'Cannot use %s wheels: %q must be a directory without a symlink; acquire this set with the same WHEELHOUSE selection.\n' "$profile" "$directory" >&2
        return 2
    fi
}
