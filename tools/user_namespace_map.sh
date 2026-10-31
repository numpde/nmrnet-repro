# Convert an ID in the caller's host namespace to the container ID that maps to
# it. The selected Engine process's map files are read by the host caller.
linux_namespace_is_initial() {
    local filesystem_type=$1 inode=$2 initial_inode=$3
    [[ $filesystem_type == 6e736673 && $inode == "$initial_inode" ]]
}

require_initial_user_and_pid_namespaces() {
    local user_fs user_inode pid_fs pid_inode
    user_fs=$(stat -f -c '%t' /proc/self/ns/user 2>/dev/null) || return 1
    user_inode=$(stat -Lc '%i' /proc/self/ns/user 2>/dev/null) || return 1
    pid_fs=$(stat -f -c '%t' /proc/self/ns/pid 2>/dev/null) || return 1
    pid_inode=$(stat -Lc '%i' /proc/self/ns/pid 2>/dev/null) || return 1
    # Linux UAPI USER_NS_INIT_INO and PID_NS_INIT_INO, after nsfs verification.
    linux_namespace_is_initial "$user_fs" "$user_inode" 4026531837 \
        && linux_namespace_is_initial "$pid_fs" "$pid_inode" 4026531836
}

probe_process_starttime() {
    local pid=$1 starttime
    [[ $pid =~ ^[1-9][0-9]*$ ]] || return 1
    starttime=$(awk '{ sub(/^.*\) /, ""); print $20 }' "/proc/$pid/stat" 2>/dev/null) || return 1
    [[ $starttime =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$starttime"
}

probe_process_has_token() {
    local pid=$1 token=$2
    [[ $pid =~ ^[1-9][0-9]*$ && $token =~ ^[[:alnum:]_-]+$ ]] \
        && grep -zFqx -- "$token" "/proc/$pid/cmdline" 2>/dev/null
}

probe_process_is_same() {
    local pid=$1 token=$2 starttime=$3 current_starttime
    probe_process_has_token "$pid" "$token" || return 1
    current_starttime=$(probe_process_starttime "$pid") || return 1
    [[ $current_starttime == "$starttime" ]]
}

probe_process_is_retired() {
    local pid=$1 starttime=$2 current_starttime
    [[ $pid =~ ^[1-9][0-9]*$ && $starttime =~ ^[0-9]+$ ]] || return 2
    [[ -e /proc/$pid/stat ]] || return 0
    current_starttime=$(probe_process_starttime "$pid") || return 2
    [[ $current_starttime != "$starttime" ]]
}

wait_for_probe_process_retirement() {
    local pid=$1 starttime=$2 attempt state
    for ((attempt = 0; attempt < 20; attempt++)); do
        if probe_process_is_retired "$pid" "$starttime"; then
            return 0
        else
            state=$?
            [[ $state == 1 ]] || return 2
        fi
        sleep 0.1
    done
    return 1
}

container_id_for_host_id() {
    local map_file=$1 host_id=$2 mapped
    [[ $host_id =~ ^[0-9]+$ && -r $map_file ]] || return 2
    mapped=$(awk -v target="$host_id" '
        function decimal(value) { return value ~ /^(0|[1-9][0-9]*)$/ }
        NF != 3 || !decimal($1) || !decimal($2) || !decimal($3) || $3 == 0 { invalid=1; next }
        {
            container_start=$1 + 0
            host_start=$2 + 0
            range_length=$3 + 0
            if (target >= host_start && target < host_start + range_length) {
                result=container_start + target - host_start
                matches++
            }
        }
        END {
            if (invalid || matches != 1) exit 1
            printf "%.0f\n", result
        }
    ' "$map_file") || return 2
    [[ $mapped =~ ^(0|[1-9][0-9]*)$ ]] || return 2
    printf '%s\n' "$mapped"
}

host_id_for_container_id() {
    local map_file=$1 container_id=$2 mapped
    [[ $container_id =~ ^[0-9]+$ && -r $map_file ]] || return 2
    mapped=$(awk -v target="$container_id" '
        function decimal(value) { return value ~ /^(0|[1-9][0-9]*)$/ }
        NF != 3 || !decimal($1) || !decimal($2) || !decimal($3) || $3 == 0 { invalid=1; next }
        {
            container_start=$1 + 0
            host_start=$2 + 0
            range_length=$3 + 0
            if (target >= container_start && target < container_start + range_length) {
                result=host_start + target - container_start
                matches++
            }
        }
        END {
            if (invalid || matches != 1) exit 1
            printf "%.0f\n", result
        }
    ' "$map_file") || return 2
    [[ $mapped =~ ^(0|[1-9][0-9]*)$ ]] || return 2
    printf '%s\n' "$mapped"
}
