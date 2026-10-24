#!/usr/bin/env bash
# The finite helper's creator compiles one original grant declaration before
# dispatch. The shared comparison consumes that material, including for
# name adoption after a lost reply. Acknowledged IDs retain their independent
# creator-owned cleanup authority; rejected execution must not leak an allocation.

deployment_helper_profile() {
    local memory=536870912 cpu_count=2 pids=64 tmpfs index expression
    tmpfs="rw,nosuid,nodev,noexec,size=64m,mode=0700,uid=$caller_uid,gid=$caller_gid"
    deployment_helper_profile_args=(--init --pull never --network none --read-only --restart no --ipc private --cgroupns private
        --user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true
        --cpus "$cpu_count" --memory "$memory" --memory-swap "$memory" --pids-limit "$pids"
        --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 --log-opt mode=non-blocking --log-opt max-buffer-size=1m
        --tmpfs "/tmp:$tmpfs")
    # This fixed local projection spells out every field, including empty grants.
    # Its order generates both the retained expectation and the Engine template;
    # there is no separately maintained row of anonymous zero-valued checks.
    local -a fields=(
        '.HostConfig.RestartPolicy.Name' no
        '.HostConfig.NetworkMode' none
        '.HostConfig.ReadonlyRootfs' true
        '.HostConfig.Privileged' false
        '.HostConfig.Init' true
        '.HostConfig.Memory' "$memory"
        '.HostConfig.MemorySwap' "$memory"
        '.HostConfig.NanoCpus' "$((cpu_count * 1000000000))"
        '.HostConfig.PidsLimit' "$pids"
        'json .HostConfig.CapDrop' '["ALL"]'
        'len .HostConfig.CapAdd' 0
        'json .HostConfig.SecurityOpt' '["no-new-privileges:true"]'
        '.HostConfig.IpcMode' private
        '.HostConfig.CgroupnsMode' private
        '.HostConfig.PidMode' ''
        '.HostConfig.UTSMode' ''
        '.HostConfig.UsernsMode' ''
        '.HostConfig.LogConfig.Type' json-file
        'json .HostConfig.LogConfig.Config' '{"max-buffer-size":"1m","max-file":"3","max-size":"10m","mode":"non-blocking"}'
        'len .HostConfig.Devices' 0
        'len .HostConfig.DeviceRequests' 0
        'len .HostConfig.DeviceCgroupRules' 0
        'len .HostConfig.GroupAdd' 0
        'len .HostConfig.ExtraHosts' 0
        'len .HostConfig.Dns' 0
        'len .HostConfig.DnsOptions' 0
        'len .HostConfig.DnsSearch' 0
        'len .HostConfig.Links' 0
        'len .HostConfig.VolumesFrom' 0
        'len .HostConfig.PortBindings' 0
        '.HostConfig.PublishAllPorts' false
        'len .HostConfig.Sysctls' 0
        'len .HostConfig.Binds' 0
        'json .HostConfig.Tmpfs' "{\"/tmp\":\"$tmpfs\"}"
    )
    deployment_helper_profile_expected= deployment_helper_profile_format=
    for ((index=0; index<${#fields[@]}; index+=2)); do
        deployment_helper_profile_expected+=${fields[index+1]}\|
        expression=${fields[index]}
        if [[ $expression == 'len '* ]]; then
            expression=${expression#len }
            # Engine omits some empty optional collections. Missing/null and
            # empty each grant zero entries; nonempty values retain their count.
            expression="(index .HostConfig \"${expression#.HostConfig.}\")"
            deployment_helper_profile_format+="{{if $expression}}{{len $expression}}{{else}}0{{end}}|"
        else deployment_helper_profile_format+="{{$expression}}|"; fi
    done
    deployment_helper_profile_expected=${deployment_helper_profile_expected%|}
    deployment_helper_profile_format=${deployment_helper_profile_format%|}
    # Unset/empty StopSignal means Docker's SIGTERM default. Preserve the same
    # meaning at image declaration and container observation, without touching it.
    deployment_helper_defaults_format='{{printf "%s%c%s%c%s%c%s%c" (json (index .Config "Env")) 0 (or (index .Config "StopSignal") "SIGTERM") 0 (json (index .Config "Healthcheck")) 0 (json (index .Config "WorkingDir")) 0}}'
}

deployment_helper_declare() {
    local directory=$1 phase=$2 name=$3 budget=$6 index spec rest source target writable
    local -n declared_grants=$4 declared_arguments=$5
    local -a image=() expected=() binds=()
    deployment_helper_profile
    deployment_command "$phase-image-defaults" "$budget" "$directory/image-defaults" false image inspect --format \
        '{{printf "%s%c" .Id 0}}'"$deployment_helper_defaults_format" "$controller_image_id" || return $?
    mapfile -d '' -t image < "$directory/image-defaults/response.stdout"
    (( ${#image[@]} == 6 )) && [[ ${image[0]} == "$controller_image_id" && ${image[5]} == $'\n' ]] || return 2
    deployment_helper_mount_args=(--mount "type=bind,src=$deployment_tool_source,dst=/source/tools/provider_deployment.py,readonly"
        "${declared_grants[@]}")
    (( ${#deployment_helper_mount_args[@]} % 2 == 0 )) || return 2
    for ((index=0; index<${#deployment_helper_mount_args[@]}; index+=2)); do
        [[ ${deployment_helper_mount_args[index]} == --mount ]] || return 2
        spec=${deployment_helper_mount_args[index+1]}
        [[ $spec == type=bind,src=*,dst=* ]] || return 2
        rest=${spec#type=bind,src=} source=${spec#type=bind,src=}
        source=${source%%,dst=*} rest=${rest#*,dst=}
        writable=true
        if [[ $rest == *,readonly ]]; then writable=false; rest=${rest%,readonly}; fi
        target=$rest
        [[ $source == /* && $target == /* && $source != *','* && $target != *','* ]] || return 2
        binds+=(bind "$source" "$target" "$writable" rprivate)
    done
    expected=("/$name" "$controller_image_id" "$caller_uid:$caller_gid" "$operation_generation" "$phase"
        "$repository_root" "$deployment_name" "$operation_owner" "$deployment_helper_profile_expected" "${image[@]:1:4}"
        1 /opt/provider/environment/bin/python "$((3 + ${#declared_arguments[@]}))" -I -B /source/tools/provider_deployment.py
        "${declared_arguments[@]}" "$((${#binds[@]} / 5))" "${binds[@]}")
    (
        umask 077
        set -o noclobber
        printf '%s\0' "${expected[@]}" > "$directory/request.pending" || exit $?
        sync -f -- "$directory/request.pending" || exit $?
        [[ ! -e $directory/request && ! -L $directory/request ]] || exit 2
        mv -- "$directory/request.pending" "$directory/request" || exit $?
        sync -f -- "$directory/request" "$directory" || exit $?
    ) || return $?
}

deployment_helper_inspection_format() {
    deployment_helper_profile
    deployment_helper_format='{{printf "%s%c%s%c%t%c%t%c%v%c" .Id 0 .State.Status 0 .State.Running 0 .State.Restarting 0 .State.Pid 0}}'
    deployment_helper_format+='{{printf "%s%c%s%c%s%c" .Name 0 .Image 0 .Config.User 0}}'
    deployment_helper_format+='{{printf "%s%c%s%c%s%c%s%c%s%c" (index .Config.Labels "io.nmrnet-repro.operation") 0 (index .Config.Labels "io.nmrnet-repro.helper") 0 (index .Config.Labels "io.nmrnet-repro.checkout") 0 (index .Config.Labels "io.nmrnet-repro.deployment") 0 (index .Config.Labels "io.nmrnet-repro.operation-owner") 0}}'
    deployment_helper_format+=$deployment_helper_profile_format'{{printf "%c" 0}}'
    deployment_helper_format+=$deployment_helper_defaults_format
    deployment_helper_format+='{{printf "%d%c" (len .Config.Entrypoint) 0}}{{range .Config.Entrypoint}}{{printf "%s%c" . 0}}{{end}}'
    deployment_helper_format+='{{printf "%d%c" (len .Config.Cmd) 0}}{{range .Config.Cmd}}{{printf "%s%c" . 0}}{{end}}'
    deployment_helper_format+='{{printf "%d%c" (len .Mounts) 0}}{{range .Mounts}}{{printf "%s%c%s%c%s%c%t%c%s%c" .Type 0 .Source 0 .Destination 0 .RW 0 .Propagation 0}}{{end}}'
}

deployment_helper_compare() {
    local request=$1 observation=$2 index expected_index actual_index count actual_count collection offset found bind part source tmpfs=0
    local -a expected=() actual=() matched=()
    local -a fields=(name image user operation helper checkout deployment owner containment/resources environment stop-signal healthcheck working-directory)
    local -a process_fields=(id state running restarting pid)
    expected_index=${#fields[@]} actual_index=$((${#fields[@]} + ${#process_fields[@]}))
    deployment_admitted_helper_id=
    for source in "$request" "$observation"; do
        [[ -f $source && ! -L $source && $(stat -c '%u:%a' -- "$source") == "$caller_uid:600" ]] \
            && (( $(stat -c '%s' -- "$source") < 2097152 )) || return 2
    done
    cmp -s <(tail -c 1 -- "$request") <(printf '\0') || return 2
    mapfile -d '' -t expected < "$request"
    mapfile -d '' -t actual < "$observation"
    (( ${#expected[@]} >= expected_index + 3 && ${#actual[@]} >= actual_index + 4 )) && [[ ${actual[-1]} == $'\n' ]] || return 2
    unset 'actual[-1]'
    [[ ${actual[0]} =~ ^[0-9a-f]{64}$ && ${actual[1]} =~ ^(created|running|restarting|exited|dead|paused|removing)$ \
        && ${actual[2]} =~ ^(true|false)$ && ${actual[3]} =~ ^(true|false)$ && ${actual[4]} =~ ^[0-9]+$ ]] || return 2
    for ((index=0; index<${#fields[@]}; index++)); do
        if [[ ${expected[index]} != "${actual[index+${#process_fields[@]}]}" ]]; then
            deployment_refuse "helper ${fields[index]} differs from its retained original request; the realized object remains untouched."
            return 2
        fi
    done
    for collection in entrypoint command; do
        count=${expected[expected_index]} actual_count=${actual[actual_index]}
        [[ $count =~ ^(0|[1-9][0-9]{0,5})$ && $actual_count == "$count" ]] \
            && (( expected_index + count < ${#expected[@]} && actual_index + count < ${#actual[@]} )) \
            || { deployment_refuse "helper $collection count differs from its original request."; return 2; }
        for ((offset=1; offset<=count; offset++)); do
            [[ ${expected[expected_index+offset]} == "${actual[actual_index+offset]}" ]] \
                || { deployment_refuse "helper $collection differs from its original request; argument contents remain protected."; return 2; }
        done
        expected_index=$((expected_index + count + 1)) actual_index=$((actual_index + count + 1))
    done
    count=${expected[expected_index]} actual_count=${actual[actual_index]}
    [[ $count =~ ^(0|[1-9][0-9]{0,5})$ && $actual_count =~ ^(0|[1-9][0-9]{0,5})$ ]] \
        && (( ${#expected[@]} == expected_index + 1 + count * 5 && ${#actual[@]} == actual_index + 1 + actual_count * 5 \
            && actual_count >= count && actual_count <= count + 1 )) || return 2
    for ((index=0; index<actual_count; index++)); do
        offset=$((actual_index + 1 + index * 5))
        if [[ ${actual[offset]} == tmpfs ]]; then
            [[ ${actual[offset+1]} == '' && ${actual[offset+2]} == /tmp && ${actual[offset+3]} == true \
                && ( ${actual[offset+4]} == '' || ${actual[offset+4]} == rprivate ) ]] || return 2
            tmpfs=$((tmpfs + 1)); (( tmpfs == 1 )) || return 2
            continue
        fi
        found=false
        for ((bind=0; bind<count; bind++)); do
            [[ ${matched[bind]-false} == false ]] || continue
            found=true
            for ((part=0; part<5; part++)); do
                if [[ ${actual[offset+part]} != "${expected[expected_index+1+bind*5+part]}" ]]; then found=false; break; fi
            done
            if [[ $found == true ]]; then matched[bind]=true; break; fi
        done
        [[ $found == true ]] || { deployment_refuse 'helper bind grants differ from its original request.'; return 2; }
    done
    (( ${#matched[@]} == count )) || return 2
    deployment_admitted_helper_id=${actual[0]}
    deployment_admitted_helper_state=${actual[1]}
    deployment_admitted_helper_running=${actual[2]}
    deployment_admitted_helper_restarting=${actual[3]}
    deployment_admitted_helper_pid=${actual[4]}
}

deployment_helper_admit() {
    local directory=$1 phase=$2 target=$3 receipts=$4 budget=$5
    deployment_helper_inspection_format
    deployment_command "$phase" "$budget" "$receipts" false inspect --type container --format \
        "$deployment_helper_format" "$target" || return $?
    deployment_helper_compare "$directory/request" "$receipts/response.stdout"
}

deployment_realized_helper() {
    # This is the original received realization, not a second live-ID registry.
    # It proves past identity only; cleanup still observes current physical state.
    local directory=$1 snapshot chosen= identifier=
    if [[ -e $directory/realization || -L $directory/realization ]]; then
        deployment_helper_compare "$directory/request" "$directory/realization" || return 2
        deployment_created_helper_id=$deployment_admitted_helper_id
        return 0
    fi
    # Recover a fully received proof if the caller died before its promotion.
    # A failed comparison supplies no identity; conflicting admitted IDs block.
    for snapshot in "$directory"/recovery-*/admit/response.stdout; do
        [[ -e $snapshot || -L $snapshot ]] || continue
        [[ $(realpath -m -- "$snapshot") == "$snapshot" ]] || return 2
        if deployment_helper_compare "$directory/request" "$snapshot"; then
            if [[ -n $identifier && $identifier != "$deployment_admitted_helper_id" ]]; then
                deployment_refuse 'helper observations identify different realized containers; preserve the original evidence.'
                return 2
            fi
            identifier=$deployment_admitted_helper_id chosen=$snapshot
        fi
    done
    [[ -n $chosen ]] || return 1
    deployment_helper_identity_agrees "$directory" "$identifier" || return $?
    mv -- "$chosen" "$directory/realization" || return $?
    sync -f -- "$directory/realization" "$directory" "${chosen%/*}" || return $?
    deployment_created_helper_id=$identifier
}

deployment_helper_identity_agrees() {
    local directory=$1 identifier=$2 path prior
    for path in "$directory/helper.cid" "$directory/create/response.stdout"; do
        if [[ ! -e $path && ! -L $path ]]; then continue; fi
        [[ -f $path && ! -L $path && $(stat -c '%u:%a' -- "$path") == "$caller_uid:600" ]] || return 2
        prior=
        if IFS= read -r -N 64 prior < "$path" && [[ $prior =~ ^[0-9a-f]{64}$ ]] \
            && { cmp -s "$path" <(printf '%s' "$prior") || cmp -s "$path" <(printf '%s\n' "$prior"); }; then
            [[ $prior == "$identifier" ]] || { deployment_refuse 'helper has conflicting complete creation identities; preserve both original receipts.'; return 2; }
        fi
    done
}

deployment_helper_readonly_observation() {
    local directory=$1 root=$2 helper=$3 recovery=$4 intent
    [[ -d $directory && ! -L $directory && $(realpath -m -- "$directory") == "$directory" \
        && $(stat -c '%u:%a' -- "$directory") == "$caller_uid:700" ]] || return 2
    for intent in "$root/effects/$helper-recovery-$recovery-"*.intent; do
        if [[ -e $intent || -L $intent ]]; then return 1; fi
    done
}

deployment_helper_compact_observations() {
    local directory=$1 root=$2 helper=$3 previous recovery marker kind
    for previous in "$directory"/recovery-*; do
        [[ -e $previous || -L $previous ]] || continue
        [[ ${previous##*/} != recovery-observation ]] || continue
        recovery=${previous##*/recovery-}
        [[ $recovery =~ ^[0-9a-f]{32}$ ]] || return 2
        marker=$previous/readonly-settled
        if [[ ! -e $marker && ! -L $marker ]]; then continue; fi
        [[ -f $marker && ! -L $marker && $(stat -c '%u:%a' -- "$marker") == "$caller_uid:600" ]] \
            && cmp -s "$marker" <(printf 'read-only %s\n' "$recovery") || return 2
        if deployment_helper_readonly_observation "$previous" "$root" "$helper" "$recovery"; then
            deployment_helper_rotate_observation "$directory" "$root" "$helper" "$recovery" "$previous" || return $?
        fi
    done
}

deployment_helper_rotate_observation() {
    local directory=$1 root=$2 helper=$3 recovery=$4 current=$5 previous=$1/recovery-observation kind prior
    if [[ -e $previous || -L $previous ]]; then
        [[ -f $previous/readonly-settled && ! -L $previous/readonly-settled \
            && $(stat -c '%u:%a' -- "$previous/readonly-settled") == "$caller_uid:600" ]] || return 2
        IFS=' ' read -r kind prior < "$previous/readonly-settled" || return 2
        [[ $kind == read-only && $prior =~ ^[0-9a-f]{32}$ ]] \
            && cmp -s "$previous/readonly-settled" <(printf 'read-only %s\n' "$prior") || return 2
        deployment_helper_readonly_observation "$previous" "$root" "$helper" "$prior" || return 2
        rm -rf -- "$previous" || return $?
    fi
    mv -- "$current" "$previous" || return $?
    if [[ ${deployment_failed_receipt-} == "$current/"* ]]; then
        deployment_failed_receipt=$previous/${deployment_failed_receipt#"$current/"}
    fi
    sync -f -- "$previous" "$directory" || return $?
}

deployment_helper_retain_observation() {
    local directory=$1 root=$2 helper=$3 recovery=$4 current=$5
    # Only joined, read-only attempts can replace this latest-evidence slot.
    # Uncertain stop/remove intents and their original receipts stay untouched.
    [[ $inference_retirement_confirmed == true && $inference_cancellation_status == 0 ]] || return 2
    deployment_helper_readonly_observation "$current" "$root" "$helper" "$recovery" || return $?
    (umask 077; set -o noclobber; printf 'read-only %s\n' "$recovery" > "$current/readonly-settled") || return $?
    sync -f -- "$current/readonly-settled" "$current" || return $?
    deployment_helper_rotate_observation "$directory" "$root" "$helper" "$recovery" "$current"
}

deployment_helper_discover() {
    local directory=$1 helper=$2 recovery=$3 receipts=$4 deadline=$5 remaining budget name identifier
    local -a request=()
    [[ -f $directory/request && ! -L $directory/request && $(stat -c '%u:%a' -- "$directory/request") == "$caller_uid:600" ]] \
        && (( $(stat -c '%s' -- "$directory/request") < 2097152 )) \
        && cmp -s <(tail -c 1 -- "$directory/request") <(printf '\0') || return 2
    mapfile -d '' -t request < "$directory/request"
    (( ${#request[@]} >= 13 )) && [[ ${request[3]} =~ ^[0-9a-f]{32}$ && ${request[4]} == "$helper" \
        && ${request[0]} == "/nmrnet-helper-${request[3]}-$helper" ]] || return 2
    mkdir -m 700 -- "$receipts" || return $?
    name=${request[0]#/}
    remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
    budget=10; if (( remaining < budget )); then budget=$remaining; fi
    deployment_command "$helper-recovery-$recovery-name" "$budget" "$receipts/name" false \
        ps --all --no-trunc --quiet --filter "name=^/$name$" || return $?
    if [[ ! -s $receipts/name/response.stdout ]]; then
        deployment_refuse "$helper creation has no confirmed reply or realized object yet; an absent name cannot fence a delayed create."
        return 2
    fi
    IFS= read -r identifier < "$receipts/name/response.stdout" || return 2
    [[ $identifier =~ ^[0-9a-f]{64}$ ]] \
        && cmp -s "$receipts/name/response.stdout" <(printf '%s\n' "$identifier") || return 2
    remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
    budget=10; if (( remaining < budget )); then budget=$remaining; fi
    deployment_helper_admit "$directory" "$helper-recovery-$recovery-admit" "$identifier" "$receipts/admit" "$budget" || return $?
    [[ $deployment_admitted_helper_id == "$identifier" ]] || return 2
    # A partial/missing original reply grants no identity. Full identities, when
    # present, must agree with this separately admitted realization.
    deployment_helper_identity_agrees "$directory" "$identifier" || return $?
    # Transfer the already received proof instead of copying its ID into another
    # authority file. Keep it durable before any stop/remove can erase the object.
    [[ ! -e $directory/realization && ! -L $directory/realization ]] || return 2
    mv -- "$receipts/admit/response.stdout" "$directory/realization" || return $?
    sync -f -- "$directory/realization" "$directory" "$receipts/admit" || return $?
    deployment_created_helper_id=$identifier
}
