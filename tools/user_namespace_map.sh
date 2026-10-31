# Convert an ID in the caller's host namespace to the container ID that maps to
# it. The selected Engine process's map files are read by the host caller.
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
