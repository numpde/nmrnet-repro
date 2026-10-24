#!/usr/bin/env bash
# Finite host owner for one Compose deployment. Python stays in offline helpers;
# actual Engine IDs/grants are admitted before control. No per-Job host loop.
# Contract: docs/notes/021_compose_runner_architecture_20261023.html.

source "$(dirname -- "${BASH_SOURCE[0]}")/deployment_helper_grants.sh"
source "$(dirname -- "${BASH_SOURCE[0]}")/deployment_runtime_recovery.sh"

deployment_refuse() {
    printf 'Cannot %s deployment %s: %s\n' "$deployment_action" "$deployment_name" "$1" >&2
    return 2
}

deployment_private_directory() {
    local path=$1 create=$2
    [[ $(realpath -m -- "$path") == "$path" && ! -L $path ]] \
        || { deployment_refuse 'private paths must be canonical and free of symlinks.'; return 2; }
    if [[ ! -e $path && $create == true ]]; then mkdir -m 700 -- "$path" || return $?; fi
    [[ -d $path && $(stat -c '%u:%a' -- "$path") == "$caller_uid:700" ]] \
        || { deployment_refuse 'private deployment directories must belong to the caller with mode 0700.'; return 2; }
}

deployment_command() {
    local phase=$1 budget=$2 receipts=$3 mutating=$4 status receipt size
    shift 4
    [[ $phase =~ ^[A-Za-z0-9_-]+$ && $budget =~ ^[1-9][0-9]*$ && ( $mutating == true || $mutating == false ) ]] || return 2
    [[ $inference_retirement_confirmed == true ]] || return 2
    mkdir -m 700 -- "$receipts" || return $?
    if [[ $mutating == true ]]; then
        (
            umask 077
            set -o noclobber
            printf '%s\0' "$phase" "$budget" "$receipts" "$@" > "$receipts/intent" || exit $?
            [[ ! -e $operation_root/effects/$phase.intent && ! -L $operation_root/effects/$phase.intent ]] || exit 2
            mv -- "$receipts/intent" "$operation_root/effects/$phase.intent" || exit $?
            sync -f -- "$operation_root/effects/$phase.intent" "$operation_root/effects" || exit $?
        ) || return $?
    fi
    if inference_native "deployment $phase" "$budget" stdio bash -eu -c '
        receipts=$1; shift
        umask 077
        ulimit -f 2048
        set -o noclobber
        exec "$@" > "$receipts/response.stdout" 2> "$receipts/response.stderr"
    ' bash "$receipts" "${engine[@]}" "$@"; then status=0; else status=$?; fi
    deployment_command_status=$status
    deployment_diagnostic+=$inference_diagnostic
    (set -o noclobber; printf '%s\n' "$status" > "$receipts/response.status") || return $?
    sync -f -- "$receipts/response.status" "$receipts" || return $?
    [[ $inference_retirement_confirmed == true && $inference_cancellation_status == 0 ]] || return 2
    for receipt in "$receipts/response.stdout" "$receipts/response.stderr"; do
        [[ -f $receipt && ! -L $receipt && $(stat -c '%u:%a' -- "$receipt") == "$caller_uid:600" ]] || return 2
        size=$(stat -c '%s' -- "$receipt") || return $?
        [[ $size =~ ^[0-9]+$ ]] && (( size < 2097152 )) || return 2
    done
    sync -f -- "$receipts/response.stdout" "$receipts/response.stderr" "$receipts" || return $?
    if [[ $status != 0 ]]; then
        deployment_diagnostic+=" Engine request $phase failed (client status $status)."
        if [[ -z ${deployment_primary_failure_receipt-} ]]; then
            deployment_primary_failure_receipt=$receipts/response.stderr
        else deployment_additional_failure_receipt=$receipts/response.stderr; fi
    fi
    [[ $status == 0 ]]
}

deployment_helper() {
    local phase=$1
    local -n helper_grants=$2 helper_arguments=$3
    local directory=$operation_root/$phase name=nmrnet-helper-$operation_generation-$phase
    local deadline=$((SECONDS + 85)) remaining identifier result=2
    # Sixty seconds of useful work plus physical stop (5s) and four Engine
    # response observations (5s each). No phase resets this absolute allowance.
    mkdir -m 700 -- "$directory" || return $?
    [[ -f $deployment_tool_source && ! -L $deployment_tool_source ]] || { deployment_refuse 'the captured offline helper is missing or linked.'; return 2; }
    remaining=$((deadline - SECONDS - 25))
    (( remaining > 0 )) || return 2
    deployment_helper_declare "$directory" "$phase" "$name" helper_grants helper_arguments "$remaining" || return $?
    remaining=$((deadline - SECONDS - 25))
    (( remaining > 0 )) || return 2
    deployment_helper_retired=false
    deployment_command "$phase-create" "$remaining" "$directory/create" true create \
        --name "$name" --cidfile "$directory/helper.cid" \
        --label "io.nmrnet-repro.operation=$operation_generation" --label "io.nmrnet-repro.helper=$phase" \
        --label "io.nmrnet-repro.deployment=$deployment_name" --label "io.nmrnet-repro.checkout=$repository_root" \
        --label "io.nmrnet-repro.operation-owner=$operation_owner" \
        "${deployment_helper_profile_args[@]}" "${deployment_helper_mount_args[@]}" \
        --entrypoint /opt/provider/environment/bin/python \
        "$controller_image_id" -I -B /source/tools/provider_deployment.py "${helper_arguments[@]}" || return $?
    deployment_created_helper "$directory" || return $?
    identifier=$deployment_created_helper_id
    remaining=$((deadline - SECONDS - 25))
    (( remaining > 0 )) || return 2
    deployment_helper_admit "$directory" "$phase-posture" "$identifier" "$directory/posture" "$remaining" || return $?
    [[ $deployment_admitted_helper_id == "$identifier" && $deployment_admitted_helper_state == created \
        && $deployment_admitted_helper_running == false && $deployment_admitted_helper_restarting == false \
        && $deployment_admitted_helper_pid == 0 ]] || return 2
    remaining=$((deadline - SECONDS - 25))
    if (( remaining > 0 )) && deployment_command "$phase-start" "$remaining" "$directory/start" true start "$identifier"; then
        remaining=$((deadline - SECONDS - 25))
        if (( remaining > 0 )) && deployment_command "$phase-wait" "$remaining" "$directory/wait" false wait "$identifier" \
            && cmp -s "$directory/wait/response.stdout" <(printf '0\n'); then result=0; fi
    fi
    [[ $inference_retirement_confirmed == true ]] || return 2
    remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
    deployment_command "$phase-stop" "$remaining" "$directory/stop" true stop --time 5 "$identifier" || return $?
    remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
    deployment_command "$phase-stopped" "$remaining" "$directory/stopped" false inspect --type container --format \
        '{{.Id}} {{.State.Status}} {{.State.Running}} {{.State.Pid}} {{.State.Restarting}}' "$identifier" || return $?
    if ! cmp -s "$directory/stopped/response.stdout" <(printf '%s exited false 0 false\n' "$identifier") \
        && ! cmp -s "$directory/stopped/response.stdout" <(printf '%s created false 0 false\n' "$identifier"); then return 2; fi
    if [[ $result != 0 ]]; then
        # The helper's owned diagnostic stays in Docker until this receiving
        # stream succeeds. A broken sink must not erase its only useful cause.
        remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
        inference_native 'failed deployment helper diagnostics' "$remaining" stdio "${engine[@]}" logs --tail 80 "$identifier" || return $?
    fi
    remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
    deployment_command "$phase-remove" "$remaining" "$directory/remove" true rm "$identifier" || return $?
    remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
    deployment_command "$phase-absent" "$remaining" "$directory/absent" false ps --all --no-trunc --quiet --filter "id=$identifier" || return $?
    [[ ! -s $directory/absent/response.stdout ]] || return 2
    (set -o noclobber; printf 'retired %s\n' "$identifier" > "$directory/retired") || return $?
    sync -f -- "$directory/retired" "$directory" || return $?
    deployment_helper_retired=true
    return "$result"
}

deployment_created_helper() {
    # Cleanup authority follows worker_container_created: the private CID and
    # complete original create reply must agree. A failed CLI can still return
    # this cleanup handle; it never authorizes starting or replaying the helper.
    local directory=$1 candidate path
    deployment_created_helper_id=
    if [[ ! -e $directory/create && ! -L $directory/create ]]; then return 1; fi
    [[ -d $directory/create && ! -L $directory/create \
        && $(realpath -m -- "$directory/create") == "$directory/create" \
        && $(stat -c '%u:%a' -- "$directory/create") == "$caller_uid:700" ]] || return 2
    for path in "$directory/helper.cid" "$directory/create/response.stdout"; do
        if [[ ! -e $path && ! -L $path ]]; then return 1; fi
        [[ -f $path && ! -L $path && $(stat -c '%u:%a' -- "$path") == "$caller_uid:600" ]] || return 2
    done
    IFS= read -r -N 64 candidate < "$directory/helper.cid" || return 1
    [[ $candidate =~ ^[0-9a-f]{64}$ ]] \
        && { cmp -s "$directory/helper.cid" <(printf '%s' "$candidate") || cmp -s "$directory/helper.cid" <(printf '%s\n' "$candidate"); } || return 1
    deployment_helper_identity_agrees "$directory" "$candidate" || return $?
    cmp -s "$directory/create/response.stdout" <(printf '%s\n' "$candidate") || return 1
    deployment_created_helper_id=$candidate
}

deployment_selected_bundle() {
    local name
    [[ -L $deployment_root/current ]] || { deployment_refuse 'no complete bundle is selected; run provider/deployment/install.'; return 2; }
    name=$(readlink -- "$deployment_root/current") || return $?
    [[ $name =~ ^bundles/generation-[A-Za-z0-9_-]{1,64}$ ]] || { deployment_refuse 'the selected bundle locator is malformed.'; return 2; }
    selected_bundle=$deployment_root/$name
    [[ ! -L $selected_bundle && -d $selected_bundle && $(stat -c '%u:%a' -- "$selected_bundle") == "$caller_uid:500" \
        && -f $selected_bundle/complete && ! -L $selected_bundle/complete ]] \
        && cmp -s "$selected_bundle/complete" <(printf 'complete\n') \
        || { deployment_refuse 'the selected bundle is not sealed and complete.'; return 2; }
}

deployment_load_bootstrap() {
    local bundle=$1 record=$1/controller-image-id
    [[ -d $bundle && ! -L $bundle && $(realpath -m -- "$bundle") == "$bundle" \
        && $(stat -c '%u:%a' -- "$bundle") == "$caller_uid:500" ]] || return 2
    [[ -f $record && ! -L $record && $(stat -c '%u:%a:%s' -- "$record") == "$caller_uid:400:72" ]] \
        && IFS= read -r controller_image_id < "$record" \
        && [[ $controller_image_id =~ ^sha256:[0-9a-f]{64}$ ]] \
        && cmp -s "$record" <(printf '%s\n' "$controller_image_id") || return 2
    deployment_tool_source=$bundle/helper/provider_deployment.py
    [[ -f $deployment_tool_source && ! -L $deployment_tool_source \
        && $(stat -c '%u:%a' -- "$deployment_tool_source") == "$caller_uid:400" ]] || return 2
}

deployment_inventory_rows() {
    local file=$1 width=$2 index role identifier bundle image state running restarting pid grace
    local -a fields=()
    local -A seen=()
    [[ $width == 4 || $width == 9 ]] || return 2
    [[ -f $file && ! -L $file && $(stat -c '%u:%a' -- "$file") == "$caller_uid:600" ]] \
        && (( $(stat -c '%s' -- "$file") < 8192 )) || return 2
    if [[ -s $file ]]; then cmp -s <(tail -c 1 -- "$file") <(printf '\0') || return 2; fi
    mapfile -d '' -t fields < "$file"
    (( ${#fields[@]} % width == 0 && ${#fields[@]} <= 7 * width )) || return 2
    for ((index=0; index<${#fields[@]}; index+=width)); do
        role=${fields[index]} identifier=${fields[index+1]} bundle=${fields[index+2]} image=${fields[index+3]}
        [[ $role =~ ^(provider|prediction|reader|smiles|api|interpreter|store)$ ]] || return 2
        [[ ! -v seen[$role] && $identifier =~ ^[0-9a-f]{64}$ && $bundle =~ ^generation-[A-Za-z0-9_-]{1,64}$ \
            && $image =~ ^sha256:[0-9a-f]{64}$ ]] || return 2
        seen[$role]=true
        if [[ $width == 9 ]]; then
            state=${fields[index+4]} running=${fields[index+5]} restarting=${fields[index+6]} pid=${fields[index+7]} grace=${fields[index+8]}
            [[ $state =~ ^(created|running|restarting|exited|dead|paused|removing)$ && $running =~ ^(true|false)$ \
                && $restarting =~ ^(true|false)$ && $pid =~ ^[0-9]+$ ]] || return 2
            deployment_seconds "$grace" || return 2
        fi
    done
    # Publish only after the complete handoff is admitted. These maps are this
    # command's transient inspection, never a second persisted live-ID registry.
    declare -gA deployment_ids=() deployment_bundles=() deployment_images=() deployment_states=()
    declare -gA deployment_running=() deployment_restarting=() deployment_pids=() deployment_stop_seconds=()
    for ((index=0; index<${#fields[@]}; index+=width)); do
        role=${fields[index]}
        deployment_ids[$role]=${fields[index+1]} deployment_bundles[$role]=${fields[index+2]} deployment_images[$role]=${fields[index+3]}
        if [[ $width == 9 ]]; then
            deployment_states[$role]=${fields[index+4]} deployment_running[$role]=${fields[index+5]}
            deployment_restarting[$role]=${fields[index+6]} deployment_pids[$role]=${fields[index+7]} deployment_stop_seconds[$role]=${fields[index+8]}
        fi
    done
}

deployment_seconds() {
    local value=$1 LC_ALL=C
    # This limit belongs to Bash's signed 64-bit arithmetic and the five-second
    # Engine response margin; it is not an unrelated decimal-width policy.
    [[ $value =~ ^[1-9][0-9]*$ ]] && { (( ${#value} < 19 )) \
        || { (( ${#value} == 19 )) && [[ $value < 9223372036854775803 ]]; }; }
}

deployment_create_arguments() {
    local bundle=$1 role=$2
    deployment_role_create_arguments=(compose --env-file /dev/null --project-name "$deployment_project"
        --project-directory "$repository_root" --file "$bundle/compose.json"
        create --no-build --pull never --no-recreate "$role")
}

deployment_inventory() {
    local phase=$1 directory=$operation_root/$1 identifier inspection image_inspection role bundle image original snapshot path
    local -a identifiers=() images=() grants=() arguments=()
    local -A copied=() images_seen=()
    mkdir -m 700 -- "$directory" "$directory/output" "$directory/plans" || return $?
    deployment_command "$phase-query" 30 "$directory/query" false ps --all --no-trunc --quiet \
        --filter "label=com.docker.compose.project=$deployment_project" || return $?
    mapfile -t identifiers < "$directory/query/response.stdout"
    (( ${#identifiers[@]} <= 7 )) || return 2
    for identifier in "${identifiers[@]}"; do [[ $identifier =~ ^[0-9a-f]{64}$ ]] || return 2; done
    if (( ${#identifiers[@]} )); then
        deployment_command "$phase-inspect" 30 "$directory/inspect" false inspect --type container "${identifiers[@]}" || return $?
        inspection=$directory/inspect/response.stdout
    else
        inspection=$directory/empty-inspection.json
        (set -o noclobber; printf '[]\n' > "$inspection") || return $?
    fi
    grants=(--mount "type=bind,src=$directory/query/response.stdout,dst=/evidence/ids,readonly"
        --mount "type=bind,src=$inspection,dst=/evidence/containers,readonly"
        --mount "type=bind,src=$directory/output,dst=/output")
    arguments=(inventory --identifiers /evidence/ids --inspection /evidence/containers --checkout "$repository_root" \
        --deployment "$deployment_name" --output /output/inventory)
    deployment_helper "$phase-inventory" grants arguments || return $?
    [[ $deployment_helper_retired == true ]] || return 2
    deployment_inventory_rows "$directory/output/inventory" 4 || return $?
    for role in provider prediction reader smiles api interpreter store; do
        [[ -v deployment_ids[$role] ]] || continue
        bundle=${deployment_bundles[$role]} image=${deployment_images[$role]}
        if [[ ! -v copied[$bundle] ]]; then
            original=$deployment_root/bundles/$bundle
            [[ $(realpath -m -- "$original") == "$original" && -d $original && ! -L $original \
                && $(stat -c '%u:%a' -- "$original") == "$caller_uid:500" ]] || return 2
            for path in "$original/complete" "$original/compose.json"; do
                [[ -f $path && ! -L $path && $(stat -c '%u:%a' -- "$path") == "$caller_uid:400" ]] || return 2
            done
            cmp -s "$original/complete" <(printf 'complete\n') || return 2
            (( $(stat -c '%s' -- "$original/compose.json") <= 1048576 )) || return 2
            snapshot=$directory/plans/$bundle
            mkdir -m 700 -- "$snapshot" || return $?
            cp --preserve=mode -- "$original/compose.json" "$original/complete" "$snapshot/" || return $?
            chmod 500 -- "$snapshot" || return $?
            copied[$bundle]=true
        fi
        if [[ ! -v images_seen[$image] ]]; then images+=("$image"); images_seen[$image]=true; fi
    done
    chmod 500 -- "$directory/plans"
    if (( ${#images[@]} )); then
        deployment_command "$phase-images" 30 "$directory/images" false image inspect "${images[@]}" || return $?
        image_inspection=$directory/images/response.stdout
    else
        image_inspection=$directory/empty-images.json
        (set -o noclobber; printf '[]\n' > "$image_inspection") || return $?
    fi
    grants+=(--mount "type=bind,src=$image_inspection,dst=/evidence/images,readonly"
        --mount "type=bind,src=$directory/plans,dst=/plans,readonly")
    arguments=(admit --identifiers /evidence/ids --inspection /evidence/containers --images /evidence/images \
        --bundles /plans --checkout "$repository_root" --deployment "$deployment_name" --output /output/admitted)
    deployment_helper "$phase-admit" grants arguments || return $?
    [[ $deployment_helper_retired == true ]] || return 2
    deployment_inventory_rows "$directory/output/admitted" 9
}

deployment_remove_operation() {
    local directory=$1
    [[ $inference_retirement_confirmed == true && $deployment_helper_retired == true \
        && -d $directory && ! -L $directory && $(stat -c '%u:%a' -- "$directory") == "$caller_uid:700" ]] || return 2
    # Only this retired command's copied plans/receipts are writable again;
    # stable runtime leaves and retained immutable bundles are outside its root.
    find "$directory" -type d -exec chmod u+rwx -- {} + || return $?
    rm -rf -- "$directory"
}

deployment_effects_settled() {
    # A failed read/readiness command can leave every dispatched mutation known
    # complete. Its original receipts suffice; do not ask an operator to delete
    # them, replay work, or confuse command failure with an unresolved effect.
    # Missing/failed mutation replies still require observation and fencing.
    local root=$1 intent
    [[ -d $root/effects && ! -L $root/effects && $(stat -c '%u:%a' -- "$root/effects") == "$caller_uid:700" ]] || return 2
    for intent in "$root/effects/"* "$root/effects/".[!.]* "$root/effects/"..?*; do
        [[ -e $intent || -L $intent ]] || continue
        deployment_effect_settled "$root" "$intent" || return $?
    done
}

deployment_effect_settled() {
    local root=$1 intent=$2 phase budget receipts status helper identifier
    local -a fields=()
    [[ -f $intent && ! -L $intent && $(stat -c '%u:%a' -- "$intent") == "$caller_uid:600" ]] \
        && (( $(stat -c '%s' -- "$intent") < 65536 )) \
        && cmp -s <(tail -c 1 -- "$intent") <(printf '\0') || return 2
    mapfile -d '' -t fields < "$intent"
    (( ${#fields[@]} >= 4 )) || return 2
    phase=${fields[0]} budget=${fields[1]} receipts=${fields[2]}
    [[ $phase =~ ^[A-Za-z0-9_-]+$ && $intent == "$root/effects/$phase.intent" \
        && $receipts == "$root/"* && $(realpath -m -- "$receipts") == "$receipts" ]] || return 2
    deployment_seconds "$budget" || return 2
    if [[ $phase =~ ^(.+)-recovery-[0-9a-f]{32}-(stop|remove)$ ]]; then
        helper=${BASH_REMATCH[1]}
    elif [[ $phase =~ ^(.+)-(create|start|stop|remove)$ ]]; then
        helper=${BASH_REMATCH[1]}
    else helper=; fi
    if [[ -n $helper ]]; then
        status=$root/$helper/retired
        if [[ -f $status && ! -L $status && $(stat -c '%u:%a' -- "$status") == "$caller_uid:600" ]]; then
            IFS=' ' read -r status identifier < "$root/$helper/retired" || return 2
            if [[ $status == retired && $identifier =~ ^[0-9a-f]{64}$ ]] \
                && cmp -s "$root/$helper/retired" <(printf 'retired %s\n' "$identifier"); then
                return 0
            fi
        fi
        # Successful helper create/start replies acknowledge those effects,
        # not retirement of the finite helper or release of its grants.
        return 2
    fi
    if [[ $phase =~ ^create-(api|interpreter|store|prediction|reader|smiles|provider)$ \
        && -f $receipts/reconciled && ! -L $receipts/reconciled \
        && $(stat -c '%u:%a' -- "$receipts/reconciled") == "$caller_uid:600" ]] \
        && cmp -s "$receipts/reconciled" <(printf 'realized\n'); then return 0; fi
    if deployment_runtime_phase "$phase" \
        && [[ -f $receipts/reconciled && ! -L $receipts/reconciled \
            && $(stat -c '%u:%a' -- "$receipts/reconciled") == "$caller_uid:600" ]] \
        && cmp -s "$receipts/reconciled" <(printf 'fenced\n'); then return 0; fi
    status=$receipts/response.status
    [[ -f $status && ! -L $status && $(stat -c '%u:%a' -- "$status") == "$caller_uid:600" ]] \
        && cmp -s "$status" <(printf '0\n') || return 2
}

deployment_recover_helper_attempt() {
    local helper=$1 directory=$2 recovery=$3 path=$4 deadline=$5 identifier=$6 remaining budget present
    if [[ -n $identifier ]]; then
        mkdir -m 700 -- "$path" || return $?
    else
        deployment_helper_discover "$directory" "$helper" "$recovery" "$path" "$deadline" || return $?
        identifier=$deployment_created_helper_id
    fi
    remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
    budget=10; if (( remaining < budget )); then budget=$remaining; fi
    deployment_command "$helper-recovery-$recovery-query" "$budget" "$path/query" false \
        ps --all --no-trunc --quiet --filter "id=$identifier" || return $?
    present=false
    if [[ -s $path/query/response.stdout ]]; then
        cmp -s "$path/query/response.stdout" <(printf '%s\n' "$identifier") || return 2
        present=true
    fi
    if [[ $present == true ]]; then
        remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
        budget=10; if (( remaining < budget )); then budget=$remaining; fi
        deployment_command "$helper-recovery-$recovery-stop" "$budget" "$path/stop" true \
            stop --time 5 "$identifier" || return $?
        remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
        budget=10; if (( remaining < budget )); then budget=$remaining; fi
        deployment_command "$helper-recovery-$recovery-stopped" "$budget" "$path/stopped" false inspect --type container --format \
            '{{.Id}} {{.State.Status}} {{.State.Running}} {{.State.Pid}} {{.State.Restarting}}' "$identifier" || return $?
        if ! cmp -s "$path/stopped/response.stdout" <(printf '%s exited false 0 false\n' "$identifier") \
            && ! cmp -s "$path/stopped/response.stdout" <(printf '%s created false 0 false\n' "$identifier"); then return 2; fi
        remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
        # Keep the existing protected receiving route before discarding the
        # helper's only logs. A broken sink retains the stopped helper.
        if ! inference_native 'interrupted deployment helper diagnostics' "$remaining" stdio \
            "${engine[@]}" logs --tail 80 "$identifier"; then
            deployment_diagnostic+=$inference_diagnostic
            return 2
        fi
        remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
        budget=10; if (( remaining < budget )); then budget=$remaining; fi
        deployment_command "$helper-recovery-$recovery-remove" "$budget" "$path/remove" true rm "$identifier" || return $?
    fi
    remaining=$((deadline - SECONDS)); (( remaining > 0 )) || return 2
    budget=10; if (( remaining < budget )); then budget=$remaining; fi
    deployment_command "$helper-recovery-$recovery-absent" "$budget" "$path/absent" false \
        ps --all --no-trunc --quiet --filter "id=$identifier" || return $?
    [[ ! -s $path/absent/response.stdout ]] || return 2
    (set -o noclobber; printf 'retired %s\n' "$identifier" > "$directory/retired") || return $?
    sync -f -- "$directory/retired" "$directory" || return $?
}

deployment_recover_helpers() {
    local root=$1 intent helper directory identifier recovery phase path status retained
    local deadline=$((SECONDS + 60))
    local -a fields=()
    [[ $root == "$operation_root" && -d $root/effects && ! -L $root/effects ]] || return 2
    for intent in "$root/effects/"*-create.intent; do
        [[ -e $intent || -L $intent ]] || continue
        [[ -f $intent && ! -L $intent && $(stat -c '%u:%a' -- "$intent") == "$caller_uid:600" ]] \
            && (( $(stat -c '%s' -- "$intent") < 65536 )) \
            && cmp -s <(tail -c 1 -- "$intent") <(printf '\0') || return 2
        mapfile -d '' -t fields < "$intent"
        (( ${#fields[@]} >= 4 )) && [[ ${fields[3]} == create ]] || return 2
        phase=${fields[0]} helper=${fields[0]%-create}
        [[ $phase == "$helper-create" && $helper =~ ^[A-Za-z0-9_-]+$ \
            && $intent == "$root/effects/$phase.intent" && ${fields[2]} == "$root/$helper/create" ]] || return 2
        directory=$root/$helper
        [[ -d $directory && ! -L $directory && $(realpath -m -- "$directory") == "$directory" \
            && $(stat -c '%u:%a' -- "$directory") == "$caller_uid:700" ]] || return 2
        if [[ -f $directory/retired && ! -L $directory/retired ]]; then continue; fi
        if deployment_created_helper "$directory"; then identifier=$deployment_created_helper_id
        else
            status=$?; [[ $status == 1 ]] || return "$status"
            if deployment_realized_helper "$directory"; then identifier=$deployment_created_helper_id
            else status=$?; [[ $status == 1 ]] || return "$status"; identifier=; fi
        fi
        deployment_helper_compact_observations "$directory" "$root" "$helper" || return $?
        IFS= read -r recovery < /proc/sys/kernel/random/uuid
        recovery=${recovery//-/}
        [[ $recovery =~ ^[0-9a-f]{32}$ ]] || return 2
        path=$directory/recovery-$recovery
        if deployment_recover_helper_attempt "$helper" "$directory" "$recovery" "$path" "$deadline" "$identifier"; then :
        else
            status=$?
            if [[ -d $path && $inference_retirement_confirmed == true && $inference_cancellation_status == 0 ]]; then
                if deployment_helper_retain_observation "$directory" "$root" "$helper" "$recovery" "$path"; then :
                else retained=$?; [[ $retained == 1 ]] || return "$retained"; fi
            fi
            return "$status"
        fi
    done
    deployment_effects_settled "$root"
}

deployment_recover_creates() {
    local root=$1 intent role bundle original_bundle= digest= receipt
    local -a fields=()
    local -A pending=()
    [[ $root == "$operation_root" ]] || return 2
    for intent in "$root/effects/"create-*.intent; do
        [[ -e $intent || -L $intent ]] || continue
        [[ -f $intent && ! -L $intent && $(stat -c '%u:%a' -- "$intent") == "$caller_uid:600" ]] \
            && (( $(stat -c '%s' -- "$intent") < 65536 )) \
            && cmp -s <(tail -c 1 -- "$intent") <(printf '\0') || return 2
        mapfile -d '' -t fields < "$intent"
        (( ${#fields[@]} == 18 )) || return 2
        role=${fields[0]#create-}
        [[ $role =~ ^(api|interpreter|store|prediction|reader|smiles|provider)$ \
            && $intent == "$root/effects/create-$role.intent" ]] || return 2
        if [[ -z $digest ]]; then
            digest=$(printf '%s\0%s' "$repository_root" "$deployment_name" | sha256sum) || return $?
            deployment_project=nmrnet-${digest:0:20}
        fi
        receipt=$root/create-$role
        bundle=${fields[11]%/compose.json}
        [[ $bundle == "$deployment_root/bundles/${bundle##*/}" \
            && ${bundle##*/} =~ ^generation-[A-Za-z0-9_-]{1,64}$ \
            && $(realpath -m -- "$bundle") == "$bundle" ]] || return 2
        deployment_create_arguments "$bundle" "$role"
        cmp -s "$intent" <(printf '%s\0' "create-$role" 60 "$receipt" "${deployment_role_create_arguments[@]}") || return 2
        if [[ -f $receipt/response.status && ! -L $receipt/response.status \
            && $(stat -c '%u:%a' -- "$receipt/response.status") == "$caller_uid:600" ]] \
            && cmp -s "$receipt/response.status" <(printf '0\n'); then continue; fi
        if [[ -f $receipt/reconciled && ! -L $receipt/reconciled \
            && $(stat -c '%u:%a' -- "$receipt/reconciled") == "$caller_uid:600" ]] \
            && cmp -s "$receipt/reconciled" <(printf 'realized\n'); then continue; fi
        [[ -z $original_bundle || $original_bundle == "$bundle" ]] || return 2
        original_bundle=$bundle pending[$role]=true
    done
    (( ${#pending[@]} )) || return 2
    deployment_load_bootstrap "$original_bundle" || return $?
    # One replaceable receiving root, below the original operation. Its finite
    # helpers must settle before replacement; no retry history or live-ID ledger.
    local operation_root=$root/create-recovery
    if [[ -e $operation_root || -L $operation_root ]]; then
        deployment_private_directory "$operation_root" false || return $?
        deployment_effects_settled "$operation_root" || deployment_recover_helpers "$operation_root" || return 2
        deployment_remove_operation "$operation_root" || return $?
    fi
    mkdir -m 700 -- "$operation_root" "$operation_root/effects" || return $?
    deployment_inventory observation || return $?
    for role in "${!pending[@]}"; do
        [[ -v deployment_ids[$role] && ${deployment_bundles[$role]} == "${original_bundle##*/}" ]] \
            || { deployment_refuse "$role creation has no admitted original realization yet; an absent or foreign object cannot settle the pending create."; return 2; }
    done
    # Inventory retains the received full-ID/grant proof before these markers.
    # Creation establishes identity only; recovery never starts the object.
    for role in "${!pending[@]}"; do
        receipt=$root/create-$role
        (set -o noclobber; printf 'realized\n' > "$receipt/reconciled") || return $?
        sync -f -- "$receipt/reconciled" "$receipt" || return $?
    done
    deployment_effects_settled "$root"
}

deployment_begin_operation() {
    local uuid
    deployment_recovery_observed=false
    IFS= read -r uuid < /proc/sys/kernel/random/uuid
    operation_generation=${uuid//-/}
    [[ $operation_generation =~ ^[0-9a-f]{32}$ ]] || return 2
    deployment_private_directory "$deployment_root/operation" true || return $?
    if [[ $deployment_action == status || $deployment_action == logs ]]; then
        operation_owner=query
        operation_root=$deployment_root/operation/query
        # The receipt leaf is replaced after settlement; its inode cannot own
        # serialization. This retained parent is separate from mutation's lock.
        exec {deployment_query_lock}< "$deployment_root/operation"
        flock --nonblock "$deployment_query_lock" || { deployment_refuse 'another query owns its finite inspection; no request was queued.'; return 2; }
        [[ $(stat -Lc '%d:%i' -- "/proc/$BASHPID/fd/$deployment_query_lock") == $(stat -c '%d:%i' -- "$deployment_root/operation") ]] || return 2
        deployment_private_directory "$operation_root" true || return $?
        if [[ -f $operation_root/retired && ! -L $operation_root/retired \
            && $(stat -c '%u:%a' -- "$operation_root/retired") == "$caller_uid:600" ]] \
            && cmp -s "$operation_root/retired" <(printf 'retired\n'); then
            deployment_remove_operation "$operation_root" || return $?
            mkdir -m 700 -- "$operation_root"
        elif [[ -e $operation_root/intent || -L $operation_root/intent ]]; then
            if deployment_effects_settled "$operation_root" || deployment_recover_helpers "$operation_root"; then
                deployment_remove_operation "$operation_root" || return $?
                mkdir -m 700 -- "$operation_root"
            else
                deployment_refuse 'the previous query has unresolved helper effects in operation/query; preserve the original receipts for recovery.'
                return 2
            fi
        fi
    else
        operation_owner=mutation
        operation_root=$deployment_root/operation/mutation
        if [[ -e $operation_root || -L $operation_root ]]; then
            [[ -d $operation_root && ! -L $operation_root && -f $operation_root/complete && ! -L $operation_root/complete \
                && $(stat -c '%u:%a' -- "$operation_root/complete") == "$caller_uid:600" ]] \
                && cmp -s "$operation_root/complete" <(printf 'complete\n') \
                || deployment_effects_settled "$operation_root" \
                || deployment_recover_helpers "$operation_root" \
                || deployment_recover_creates "$operation_root" \
                || deployment_recover_runtime "$operation_root" \
                || { deployment_refuse 'the previous command has unresolved effects; preserve its current operation receipts before another mutation.'; return 2; }
            if [[ ${deployment_recovery_observed-false} == true ]]; then return 0; fi
            deployment_remove_operation "$operation_root" || return $?
        fi
        mkdir -m 700 -- "$operation_root"
    fi
    (set -o noclobber; printf '%s\0%s\0' "$deployment_action" "$operation_generation" > "$operation_root/intent") || return $?
    mkdir -m 700 -- "$operation_root/effects" || return $?
    sync -f -- "$operation_root/intent" "$operation_root" "$deployment_root" || return $?
    operation_established=true
}

deployment_finish() {
    local status=$? cleanup=0
    trap - EXIT
    if [[ $inference_retirement_confirmed == true ]]; then
        docker_client_cleanup || cleanup=$?
    fi
    if [[ $status == 0 && $cleanup != 0 ]]; then status=$cleanup; fi
    if [[ $status != 0 && $operation_established == true && $operation_owner == query \
        && $inference_retirement_confirmed == true && $deployment_helper_retired == true ]]; then
        # A failed query can still have every helper settled. Keep that fact
        # distinct from query success so a later query need not babysit it.
        if (set -o noclobber; printf 'retired\n' > "$operation_root/retired") \
            && sync -f -- "$operation_root/retired" "$operation_root"; then :
        else deployment_diagnostic+=' Query helper retirement is known locally, but its durable receipt could not be published; preserve the original operation.'; fi
    fi
    if [[ -n ${pending_diagnostic-} ]]; then printf '%s\n' "$pending_diagnostic" >&2; fi
    if [[ -n ${deployment_diagnostic-} ]]; then printf '%s\n' "$deployment_diagnostic" >&2; fi
    if [[ -n ${deployment_primary_failure_receipt-} ]]; then printf 'Protected primary Engine cause: %s\n' "$deployment_primary_failure_receipt" >&2; fi
    if [[ -n ${deployment_additional_failure_receipt-} ]]; then printf 'Protected additional Engine cause: %s\n' "$deployment_additional_failure_receipt" >&2; fi
    if [[ $status != 0 ]]; then
        printf 'Deployment command did not complete. Original operation receipts: %s\n' "${operation_root-not-established}" >&2
    elif [[ ${deployment_recovery_observed-false} == true ]]; then
        printf 'Requested deployment state observed. Original late-request restrictions remain at %s.\n' "$operation_root"
    elif [[ $operation_owner == query ]]; then
        deployment_remove_operation "$operation_root" || status=$?
    else
        (set -o noclobber; printf 'complete\n' > "$operation_root/complete") || status=$?
        sync -f -- "$operation_root/complete" "$operation_root" || status=$?
    fi
    exit "$status"
}

deployment_bootstrap() {
    local digest hint identifier
    local -a identifiers=()
    # Discovery hint only. The contained inventory independently admits the
    # project/checkout/deployment labels before any container control.
    digest=$(printf '%s\0%s' "$repository_root" "$deployment_name" | sha256sum)
    deployment_project=nmrnet-${digest:0:20}
    deployment_command bootstrap-query 30 "$operation_root/bootstrap-query" false \
        ps --all --no-trunc --quiet --filter "label=com.docker.compose.project=$deployment_project" || return $?
    mapfile -t identifiers < "$operation_root/bootstrap-query/response.stdout"
    (( ${#identifiers[@]} <= 7 )) || { deployment_refuse 'the project has more than its seven fixed roles.'; return 2; }
    for identifier in "${identifiers[@]}"; do [[ $identifier =~ ^[0-9a-f]{64}$ ]] || return 2; done
    if (( ${#identifiers[@]} )); then
        deployment_command bootstrap-hint 30 "$operation_root/bootstrap-hint" false inspect --type container --format \
            '{{index .Config.Labels "io.nmrnet-repro.bundle"}}' "${identifiers[0]}" || return $?
        IFS= read -r hint < "$operation_root/bootstrap-hint/response.stdout" || return 2
        [[ $hint =~ ^generation-[A-Za-z0-9_-]{1,64}$ ]] \
            && cmp -s "$operation_root/bootstrap-hint/response.stdout" <(printf '%s\n' "$hint") || return 2
        deployment_load_bootstrap "$deployment_root/bundles/$hint" || { deployment_refuse 'restore the original sealed bundle/helper/image before controlling its existing containers.'; return 2; }
    elif [[ $deployment_action == up ]]; then
        deployment_selected_bundle || return $?
        deployment_load_bootstrap "$selected_bundle" || return $?
    fi
    deployment_existing_count=${#identifiers[@]}
}

deployment_capture_sources() {
    local destination=$1 capture_directory=$1/inference-build revision digest changes
    local -a selectors=() files=() inference_selectors=()
    source "$repository_root/tools/provider_image.sh"
    source "$repository_root/tools/geometry_image.sh"
    source "$repository_root/tools/inference_inputs.sh"
    repository_revision=$(repository_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}') || return $?
    mkdir -m 700 -- "$destination" "$capture_directory" "$destination/sources" || return $?
    inference_capture_inputs || return $?
    deployment_upstream_revision=$revision
    digest=$(sha256sum -- "$capture_directory/authored") || return $?
    deployment_inference_inputs=sha256:${digest%% *}
    mapfile -t inference_selectors < "$capture_directory/declared-inputs"
    selectors=("${inference_selectors[@]}" "${geometry_image_inputs[@]}" "${provider_image_inputs[@]}"
        artifacts/prediction-models.json tools/provider_deployment.sh tools/provider_deployment.py
        tools/deployment_helper_grants.sh tools/deployment_runtime_recovery.sh)
    changes=$(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- "${selectors[@]}") || return $?
    [[ -z $changes ]] || { deployment_refuse 'commit deployment/image inputs before freezing their execution provenance.'; return 2; }
    copy_git_blobs 'deployment execution sources' "$repository_root" "$repository_revision" \
        "$destination/sources" "$destination/inputs" "${selectors[@]}" || return $?
    mapfile -t files < "$destination/inputs.paths"
    (cd -- "$destination/sources"; sha256sum -- "${files[@]}") > "$destination/sources.sha256" || return $?
    printf '%s\n' "$repository_revision" > "$destination/revision"
    deployment_tool_source=$destination/sources/tools/provider_deployment.py
}

deployment_admit_images() {
    local record=$repository_root/.work/inference/image-id
    print_command=false
    require_provider_image
    controller_image_id=$provider_image_id
    require_geometry_image
    [[ -f $record && ! -L $record && $(stat -c '%s' -- "$record") == 72 ]] \
        && IFS= read -r scientific_image_id < "$record" \
        && [[ $scientific_image_id =~ ^sha256:[0-9a-f]{64}$ ]] \
        && cmp -s "$record" <(printf '%s\n' "$scientific_image_id") || return 2
    deployment_command inference-image 30 "$operation_root/inference-image" false image inspect --format '{{.Id}}' "$scientific_image_id" || return $?
    cmp -s "$operation_root/inference-image/response.stdout" <(printf '%s\n' "$scientific_image_id") || return 2
    deployment_command inference-inputs 30 "$operation_root/inference-inputs" false image inspect \
        --format '{{index .Config.Labels "io.nmrnet-repro.inference-inputs"}}' "$scientific_image_id" || return $?
    cmp -s "$operation_root/inference-inputs/response.stdout" <(printf '%s\n' "$deployment_inference_inputs") \
        || { deployment_refuse 'the inference image has different binding/core source inputs; rebuild it before installation.'; return 2; }
    deployment_command inference-upstream 30 "$operation_root/inference-upstream" false image inspect \
        --format '{{index .Config.Labels "io.nmrnet-repro.upstream-revision"}}' "$scientific_image_id" || return $?
    cmp -s "$operation_root/inference-upstream/response.stdout" <(printf '%s\n' "$deployment_upstream_revision") || return 2
}

deployment_install() {
    local bundle name input
    local -a grants=() arguments=()
    for role in provider prediction reader smiles api interpreter store; do
        if [[ -v deployment_ids[$role] && ( ${deployment_running[$role]} == true || ${deployment_restarting[$role]} == true || ${deployment_pids[$role]} != 0 ) ]]; then
            deployment_refuse 'stop the existing stack before selecting a new bundle; running work was left untouched.'
            return 2
        fi
    done
    deployment_capture_sources "$operation_root/native-inputs" || return $?
    deployment_admit_images || return $?
    deployment_private_directory "$deployment_root/bundles" true || return $?
    bundle=$(mktemp -d "$deployment_root/bundles/generation-XXXXXXXX") || return $?
    mkdir -m 700 -- "$operation_root/output" || return $?
    grants=(--mount "type=bind,src=$repository_root/config/deployments/$deployment_name/provider.toml,dst=$repository_root/config/deployments/$deployment_name/provider.toml,readonly"
        --mount "type=bind,src=$repository_root/config/deployments/$deployment_name/deployment.toml,dst=$repository_root/config/deployments/$deployment_name/deployment.toml,readonly"
        --mount "type=bind,src=$bundle,dst=$bundle"
        --mount "type=bind,src=$operation_root/native-inputs,dst=/native-inputs,readonly")
    # The credential reader receives flat project inputs, not the deployment's
    # retained bundles, runtime leaves or other command receipts.
    find "$deployment_root" -mindepth 1 -maxdepth 1 -type f -print0 > "$operation_root/flat-inputs" || return $?
    while IFS= read -r -d '' input; do
        [[ $input != *','* && $input != *$'\n'* && $input != *$'\r'* ]] || continue
        grants+=(--mount "type=bind,src=$input,dst=$input,readonly")
    done < "$operation_root/flat-inputs"
    arguments=(freeze --definition "$repository_root/config/deployments/$deployment_name/provider.toml"
        --settings "$repository_root/config/deployments/$deployment_name/deployment.toml" --secrets "$deployment_root" --bundle "$bundle"
        --native-capture /native-inputs --models /native-inputs/sources/artifacts/prediction-models.json
        --deployment "$deployment_name" --checkout "$repository_root" --uid "$caller_uid" --gid "$caller_gid"
        --prepared "$repository_root/.work/prepared" --image-id "$scientific_image_id" --geometry-image-id "$geometry_image_id"
        --controller-image-id "$controller_image_id" --repository-revision "$repository_revision" --upstream-revision "$deployment_upstream_revision")
    deployment_helper freeze grants arguments || return $?
    # Config normalization reads no credential values. Use the original recipe
    # for creation too, so literal dollars are decoded once by the same CLI.
    deployment_command normalize 30 "$operation_root/normalize" false compose --env-file /dev/null \
        --project-name "$deployment_project" --project-directory "$repository_root" --file "$bundle/compose.json" config --format json || return $?
    grants=(--mount "type=bind,src=$bundle,dst=$bundle"
        --mount "type=bind,src=$operation_root/normalize/response.stdout,dst=/normalized.json,readonly"
        --mount "type=bind,src=$operation_root/output,dst=/output")
    arguments=(finalize --bundle "$bundle" --normalized /normalized.json --output /output)
    deployment_helper finalize grants arguments || return $?
    [[ $deployment_helper_retired == true ]] || return 2
    name=${bundle##*/}
    cmp -s "$operation_root/output/bundle-name" <(printf '%s\n' "$name") || return 2
    ln -s -- "bundles/$name" "$deployment_root/.current-$operation_generation" || return $?
    mv -T -- "$deployment_root/.current-$operation_generation" "$deployment_root/current" || return $?
    sync -f -- "$deployment_root" || return $?
    printf 'Installed deployment %s from %s. Services remain stopped.\n' "$deployment_name" "$name"
}

deployment_host_path() {
    [[ $1 == /* && $1 != / && $1 != *','* && $1 != *$'\n'* && $1 != *$'\r'* \
        && $(realpath -m -- "$1") == "$1" ]] \
        || { deployment_refuse 'a retained bind path is no longer canonical or contains an unsupported delimiter.'; return 2; }
}

deployment_plan_inputs() {
    local create_store=$1 snapshot=$operation_root/selected-plan path role mode interface network index
    local -a grants=() arguments=() fields=()
    deployment_load_bootstrap "$selected_bundle" || return $?
    mkdir -m 700 -- "$snapshot" "$operation_root/manifest-output" || return $?
    cp --preserve=mode -- "$selected_bundle/compose.json" "$selected_bundle/complete" "$snapshot/" || return $?
    chmod 500 -- "$snapshot"
    grants=(--mount "type=bind,src=$snapshot,dst=/bundle,readonly"
        --mount "type=bind,src=$operation_root/manifest-output,dst=/output")
    arguments=(manifest --bundle /bundle --output /output/fields)
    deployment_helper manifest grants arguments || return $?
    [[ $deployment_helper_retired == true ]] || return 2
    path=$operation_root/manifest-output/fields
    [[ -f $path && ! -L $path && $(stat -c '%u:%a' -- "$path") == "$caller_uid:600" ]] \
        && (( $(stat -c '%s' -- "$path") < 8192 )) \
        && cmp -s <(tail -c 1 -- "$path") <(printf '\0') || return 2
    mapfile -d '' -t fields < "$path"
    (( ${#fields[@]} == 20 )) && [[ ${fields[0]} == "$deployment_project" ]] || return 2
    declare -gA deployment_readiness_seconds=()
    deployment_startup_seconds=0 index=13
    for role in api interpreter store prediction reader smiles provider; do
        deployment_seconds "${fields[index]}" || return 2
        (( fields[index] <= 9223372036854775802 - deployment_startup_seconds - 30 )) || return 2
        deployment_readiness_seconds[$role]=${fields[index]}
        deployment_startup_seconds=$((deployment_startup_seconds + fields[index] + 30))
        index=$((index + 1))
    done
    deployment_seconds "$deployment_startup_seconds" || return 2
    deployment_store=${fields[1]} deployment_weight=${fields[2]} deployment_checkpoints=${fields[3]}
    for path in "$deployment_store" "$deployment_weight" "$deployment_checkpoints"; do deployment_host_path "$path" || return $?; done
    if [[ $create_store == true ]]; then mkdir -p -- "$deployment_store" || return $?; fi
    [[ -d $deployment_store && ! -L $deployment_store ]] \
        || { deployment_refuse 'the admitted Attempt-store path is missing; a running deployment cannot create a replacement store.'; return 2; }
    [[ $(stat -c '%u:%a' -- "$deployment_store") == "$caller_uid:700" ]] || { deployment_refuse 'the Attempt store must remain caller-owned mode 0700.'; return 2; }
    [[ -d $deployment_weight && -d $deployment_checkpoints ]] || { deployment_refuse 'restore the retained prepared model roots before startup.'; return 2; }
    for role in api interpreter store; do
        case $role in api) index=4 ;; interpreter) index=7 ;; store) index=10 ;; esac
        mode=${fields[index]} interface=${fields[index+1]} network=${fields[index+2]}
        [[ $mode =~ ^(local|mapped|external)$ && $interface =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$ ]] || return 2
        if [[ $mode == local ]]; then
            [[ $network =~ ^[0-9a-f]{64}$ ]] || return 2
            deployment_command "inspect-network-$role" 30 "$operation_root/inspect-network-$role" false network inspect "$network" || return $?
            grants=(--mount "type=bind,src=$snapshot,dst=/bundle,readonly"
                --mount "type=bind,src=$operation_root/inspect-network-$role/response.stdout,dst=/network.json,readonly")
            arguments=(network --bundle /bundle --role "$role" --inspection /network.json)
            deployment_helper "network-$role" grants arguments || return $?
        else
            [[ -z $network ]] || return 2
            ip link show dev "$interface" >/dev/null || { deployment_refuse "the $role route's explicit host interface is unavailable."; return 2; }
        fi
    done
}

deployment_runtime_directories() {
    local role path lease
    deployment_private_directory "$deployment_root/runtime" true || return $?
    for role in prediction reader smiles route-api route-interpreter route-store; do
        path=$deployment_root/runtime/$role
        deployment_private_directory "$path" true || return $?
        if [[ $role == route-* ]]; then continue; fi
        lease=$path/.owner-session.lock
        if [[ ! -e $lease && ! -L $lease ]]; then (set -o noclobber; : > "$lease") || return $?; fi
        [[ -f $lease && ! -L $lease && $(stat -c '%u:%a' -- "$lease") == "$caller_uid:600" ]] \
            || { deployment_refuse 'native lifetime locks must be existing caller-owned mode 0600 files; preserve their inodes.'; return 2; }
        sync -f -- "$lease" "$path" || return $?
    done
}

deployment_stopped() {
    local role=$1 phase=$2 identifier=${deployment_ids[$1]} observed state running pid restarting exit_code oom
    deployment_command "$phase-observe-$role" 30 "$operation_root/$phase-observe-$role" false inspect --type container --format \
        '{{.Id}}|{{.State.Status}}|{{.State.Running}}|{{.State.Pid}}|{{.State.Restarting}}|{{.State.ExitCode}}|{{.State.OOMKilled}}' "$identifier" || return $?
    IFS='|' read -r observed state running pid restarting exit_code oom < "$operation_root/$phase-observe-$role/response.stdout" || return 2
    [[ $observed == "$identifier" && ( $state == exited || $state == created ) && $running == false && $pid == 0 \
        && $restarting == false && $exit_code =~ ^[0-9]+$ && $oom =~ ^(true|false)$ ]] \
        && cmp -s "$operation_root/$phase-observe-$role/response.stdout" <(printf '%s|%s|false|0|false|%s|%s\n' "$identifier" "$state" "$exit_code" "$oom") \
        || { deployment_refuse "$role has no confirmed stopped, non-restarting process state; dependent components remain available."; return 2; }
    printf '%s container stopped (exit %s; OOMKilled=%s). Durable Attempt outcomes are unchanged by this observation.\n' "$role" "$exit_code" "$oom"
}

deployment_down() {
    local role grace
    # Physical provider exit precedes native retirement, then proxy shutdown.
    # Original request settlement/fencing is a separate recovery obligation.
    for role in provider prediction reader smiles api interpreter store; do
        [[ -v deployment_ids[$role] ]] || continue
        grace=${deployment_stop_seconds[$role]}
        deployment_seconds "$grace" || return 2
        deployment_runtime_effects=true
        deployment_stop_role "$role" "$grace" || return $?
        deployment_stopped "$role" stop || return $?
        if [[ ${deployment_runtime_fence-false} == true ]]; then deployment_fence_role "$role" || return $?; fi
    done
}

deployment_up() {
    local role identifier name running_count=0
    deployment_selected_bundle || return $?
    name=${selected_bundle##*/}
    for role in provider prediction reader smiles api interpreter store; do
        if [[ -v deployment_ids[$role] && ( ${deployment_running[$role]} == true || ${deployment_restarting[$role]} == true || ${deployment_pids[$role]} != 0 ) ]]; then
            [[ ${deployment_bundles[$role]} == "$name" \
                && ( ( ${deployment_running[$role]} == true && ${deployment_restarting[$role]} == false ) \
                    || ( ${deployment_resuming-false} == true && ${deployment_restarting[$role]} == true ) ) ]] \
                || { deployment_refuse 'a different generation or restarting process still owns the stack; use down before reconfiguration.'; return 2; }
            running_count=$((running_count + 1))
        fi
    done
    if (( ${#deployment_ids[@]} )) || [[ ${deployment_resuming-false} == true ]]; then deployment_plan_inputs false || return $?
    else deployment_plan_inputs true || return $?; fi
    if (( running_count )) && [[ ${deployment_resuming-false} != true || $running_count == 7 ]]; then
        (( running_count == 7 && ${#deployment_ids[@]} == 7 )) \
            || { deployment_refuse 'only part of the stack is running; preserve original owners and use down before restarting.'; return 2; }
        deployment_start_deadline=$((SECONDS + deployment_startup_seconds))
        for role in api interpreter store prediction reader smiles provider; do deployment_ready "$role" || return $?; done
        printf 'Deployment %s remains ready from %s; no component was restarted.\n' "$deployment_name" "$name"
        return 0
    fi
    deployment_runtime_directories || return $?
    for role in provider prediction reader smiles api interpreter store; do
        [[ -v deployment_ids[$role] ]] || continue
        if [[ ${deployment_bundles[$role]} != "$name" ]]; then
            printf 'Replacing stopped %s container %s; its bounded Docker logs will be discarded.\n' "$role" "${deployment_ids[$role]}" || return $?
            deployment_runtime_effects=true
            deployment_command "replace-$role" 30 "$operation_root/replace-$role" true rm "${deployment_ids[$role]}" || return $?
        fi
    done
    deployment_runtime_effects=true
    # The six leaves have no dependencies. Admit each completed create before
    # allowing the next one; provider is last, with all its dependencies already
    # present. --no-recreate prevents its dependency traversal creating a second
    # generation. A lost reply therefore leaves only one named create to settle.
    for role in api interpreter store prediction reader smiles provider; do
        if [[ -v deployment_ids[$role] && ${deployment_bundles[$role]} == "$name" ]]; then continue; fi
        deployment_create_arguments "$selected_bundle" "$role"
        deployment_command "create-$role" 60 "$operation_root/create-$role" true "${deployment_role_create_arguments[@]}" || return $?
        deployment_inventory "created-$role" || return $?
        [[ -v deployment_ids[$role] && ${deployment_bundles[$role]} == "$name" ]] \
            || { deployment_refuse "$role creation did not yield an admitted container from the selected bundle."; return 2; }
    done
    (( ${#deployment_ids[@]} == 7 )) || { deployment_refuse 'Compose did not create exactly the seven expected roles.'; return 2; }
    deployment_start_deadline=$((SECONDS + deployment_startup_seconds))
    for role in api interpreter store prediction reader smiles provider; do
        [[ ${deployment_bundles[$role]} == "$name" ]] || return 2
        identifier=${deployment_ids[$role]}
        if [[ ${deployment_resuming-false} != true \
            || ( ${deployment_running[$role]} != true && ${deployment_restarting[$role]} != true ) ]]; then
            deployment_start_role "$role" || return $?
        fi
        deployment_ready "$role" || return $?
    done
    deployment_inventory started || return $?
    for role in provider prediction reader smiles api interpreter store; do
        [[ ${deployment_running[$role]} == true && ${deployment_restarting[$role]} == false ]] \
            || { deployment_refuse "$role did not remain running after startup; retain its Docker logs and original bundle."; return 2; }
    done
    printf 'Deployment %s is ready from %s; the provider accepted its initial Hello.\n' "$deployment_name" "$name"
}

deployment_ready() {
    local role=$1 observed running restarting health attempt=0 remaining budget
    local seconds=${deployment_readiness_seconds[$1]}
    local deadline=$((SECONDS + seconds))
    if (( deadline > deployment_start_deadline )); then deadline=$deployment_start_deadline; fi
    while (( SECONDS < deadline )); do
        remaining=$((deadline - SECONDS)) budget=5
        if (( remaining < budget )); then budget=$remaining; fi
        deployment_command "ready-$role-$attempt" "$budget" "$operation_root/ready-$role-$attempt" false inspect --type container --format \
            '{{.Id}}|{{.State.Running}}|{{.State.Restarting}}|{{if .State.Health}}{{.State.Health.Status}}{{else}}missing{{end}}' "${deployment_ids[$role]}" || return $?
        IFS='|' read -r observed running restarting health < "$operation_root/ready-$role-$attempt/response.stdout" || return 2
        [[ $observed == "${deployment_ids[$role]}" && $running =~ ^(true|false)$ && $restarting =~ ^(true|false)$ ]] \
            || { deployment_refuse "$role readiness inspection changed identity or returned malformed process state; preserve its original receipts."; return 2; }
        if [[ $running == false || $restarting == true ]]; then
            # Engine owns unless-stopped recovery. Observe this same admitted ID
            # within the original deadline; never send another start here.
            sleep 1
            attempt=$((attempt + 1))
            continue
        fi
        # Docker's cached health can outlive the record's 30s freshness bound.
        # Read the same current record now; never infer readiness from its cache.
        remaining=$((deadline - SECONDS)) budget=5
        (( remaining > 0 )) || break
        if (( remaining < budget )); then budget=$remaining; fi
        if deployment_process_status "$role" "ready-status-$role-$attempt" "$budget"; then
            if [[ ( $deployment_process_phase == ready || $deployment_process_phase == working ) \
                && $deployment_process_age -le 30000 ]]; then return 0; fi
        fi
        [[ $health == starting || $health == unhealthy || $health == healthy ]] || { deployment_refuse "$role has no admitted readiness observation."; return 2; }
        sleep 1
        attempt=$((attempt + 1))
    done
    deployment_refuse "$role readiness was not confirmed within its ${seconds}s allowance or the remaining startup budget; containers and their Docker logs are retained. No Job was resubmitted."
    return 2
}

deployment_process_status() {
    local role=$1 phase=$2 budget=$3 interpreter status_role=$1 pid ticks output
    case $role in
        provider|api|interpreter|store) interpreter=/opt/provider/environment/bin/python ;;
        prediction) interpreter=/usr/local/bin/python ;;
        reader|smiles) interpreter=/opt/rdkit/environment/bin/python ;;
        *) return 2 ;;
    esac
    case $role in api|interpreter|store) status_role=proxy ;; esac
    deployment_process_phase=unavailable deployment_process_age=0
    deployment_command "$phase" "$budget" "$operation_root/$phase" false exec "${deployment_ids[$role]}" \
        "$interpreter" -I -B -m provider_core.process_status /run/status.json "$status_role" --describe || return $?
    output=$operation_root/$phase/response.stdout
    IFS='|' read -r deployment_process_phase deployment_process_age pid ticks < "$output" || return 2
    [[ $deployment_process_phase =~ ^(ready|working|starting|stopping|held|api-unconfirmed)$ \
        && $deployment_process_age =~ ^[0-9]{1,15}$ && $pid =~ ^[1-9][0-9]*$ && $ticks =~ ^[1-9][0-9]*$ ]] \
        && cmp -s "$output" <(printf '%s|%s|%s|%s\n' "$deployment_process_phase" "$deployment_process_age" "$pid" "$ticks") || return 2
}

deployment_dispatch() {
    local role
    declare -gA deployment_ids=() deployment_bundles=() deployment_images=() deployment_states=()
    declare -gA deployment_running=() deployment_restarting=() deployment_pids=() deployment_stop_seconds=()
    if (( deployment_existing_count )); then deployment_inventory existing || return $?; fi
    case $deployment_action in
        install) deployment_install ;;
        up) deployment_up ;;
        down) deployment_down ;;
        status)
            for role in provider prediction reader smiles api interpreter store; do
                [[ -v deployment_ids[$role] ]] || continue
                printf '%s: %s; running=%s restarting=%s; bundle=%s; ID=%s\n' "$role" "${deployment_states[$role]}" \
                    "${deployment_running[$role]}" "${deployment_restarting[$role]}" "${deployment_bundles[$role]}" "${deployment_ids[$role]}"
                if [[ ${deployment_running[$role]} == true && ${deployment_restarting[$role]} == false ]]; then
                    if deployment_process_status "$role" "status-$role" 5; then
                        printf '  process observation: %s; age=%sms (expires after 30000ms).\n' "$deployment_process_phase" "$deployment_process_age"
                    else
                        printf '  process readiness observation unavailable; inspect retained command evidence and Docker logs.\n' >&2
                        return 2
                    fi
                fi
            done
            ;;
        logs)
            for role in provider prediction reader smiles api interpreter store; do
                [[ -v deployment_ids[$role] ]] || continue
                printf '%s:\n' "$role"
                inference_native "deployment $role logs" 30 stdio "${engine[@]}" logs --tail 80 "${deployment_ids[$role]}" || return $?
            done
            ;;
    esac
}

deployment_main() {
    set -euo pipefail
    umask 077
    repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
    caller_uid=$(id -u) caller_gid=$(id -g)
    deployment_action=${1-} deployment_name=${NMRNET_DEPLOYMENT_INPUT-}
    [[ $# == 1 && $deployment_action =~ ^(install|up|status|logs|down)$ ]] || { printf '%s\n' 'Use make provider/deployment/{install,up,status,logs,down} DEPLOYMENT=<name>.' >&2; return 2; }
    [[ $deployment_name =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$ ]] || { deployment_refuse 'supply an explicit DEPLOYMENT name, up to 48 letters, digits, underscores or hyphens.'; return 2; }
    [[ $caller_uid != 0 && $caller_gid != 0 ]] || { deployment_refuse 'run as the nonroot deployment owner.'; return 2; }
    [[ $repository_root != *','* && $repository_root != *$'\n'* && $repository_root != *$'\r'* ]] || { deployment_refuse 'the checkout path contains an unsupported bind delimiter.'; return 2; }
    deployment_root=$repository_root/secrets/deployments/$deployment_name
    create_private=false
    if [[ $deployment_action == install ]]; then create_private=true; fi
    for directory in "$repository_root/secrets" "$repository_root/secrets/deployments" "$deployment_root"; do
        deployment_private_directory "$directory" "$create_private" || return $?
    done
    if [[ $deployment_action == install || $deployment_action == up || $deployment_action == down ]]; then
        exec {deployment_lock}< "$deployment_root"
        flock --nonblock "$deployment_lock" || { deployment_refuse 'another command owns this deployment; no request was queued.'; return 2; }
        [[ $(stat -Lc '%d:%i' -- "/proc/$BASHPID/fd/$deployment_lock") == $(stat -c '%d:%i' -- "$deployment_root") ]] || return 2
    fi
    source "$repository_root/tools/docker_client.sh"
    source "$repository_root/tools/inference_native.sh"
    source "$repository_root/tools/git_metadata.sh"
    docker_client_directory= deployment_diagnostic= deployment_primary_failure_receipt= deployment_additional_failure_receipt= pending_diagnostic= inference_diagnostic= inference_cancellation_status=0 inference_retirement_confirmed=true
    deployment_runtime_effects=false
    unset deployment_runtime_root deployment_runtime_prefix
    deployment_resuming=false deployment_runtime_fence=false deployment_recovery_observed=false
    deployment_helper_retired=true
    operation_root= operation_owner=unestablished operation_established=false
    trap deployment_finish EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    docker_client_prepare run || { printf '%s\n' "$docker_client_diagnostic" >&2; return 2; }
    # Main graph is installed below the finite command/helper owners. Its
    # mutation entry must retain one discoverable operation root before effects.
    deployment_begin_operation
    if [[ ${deployment_recovery_observed-false} == true ]]; then return 0; fi
    deployment_bootstrap
    deployment_dispatch
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then deployment_main "$@"; fi
