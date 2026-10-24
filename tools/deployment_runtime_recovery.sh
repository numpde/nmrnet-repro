#!/usr/bin/env bash
# Persistent service effects differ from finite helpers: observed availability
# permits same-intent recovery, but an older opposing request still targets its
# original ID. Only original completion or that ID's removal fences it.

deployment_runtime_phase() {
    [[ $1 =~ ^((resume|recover)-[0-9a-f]{32}-)?(start|stop|replace|remove)-(api|interpreter|store|prediction|reader|smiles|provider)$ ]] || return 1
    deployment_runtime_kind=${BASH_REMATCH[3]} deployment_runtime_role=${BASH_REMATCH[4]}
}

deployment_start_role() {
    local role=$1 phase=${deployment_runtime_prefix-}start-$1
    local operation_root=${deployment_runtime_root-$operation_root}
    deployment_command "$phase" 30 "$operation_root/$phase" true start "${deployment_ids[$role]}"
}

deployment_stop_role() {
    local role=$1 grace=$2 phase=${deployment_runtime_prefix-}stop-$1
    local operation_root=${deployment_runtime_root-$operation_root}
    if [[ -n ${deployment_runtime_root-} && $deployment_action == down \
        && ${deployment_runtime_fence-false} == false && ${deployment_running[$role]} == false \
        && ${deployment_restarting[$role]} == false && ${deployment_pids[$role]} == 0 ]]; then
        # This root's stop restriction blocks every new start. Reuse its actual
        # acknowledged stop plus current stopped state, never a snapshot alone.
        if deployment_acknowledged_stop "$role" "$grace"; then return 0; fi
    fi
    deployment_command "$phase" "$((grace + 5))" "$operation_root/$phase" true stop --time "$grace" "${deployment_ids[$role]}"
}

deployment_acknowledged_stop() {
    local role=$1 grace=$2 intent prior
    local root=${deployment_runtime_root-$operation_root}
    for intent in "$root/effects/"*stop-$role.intent; do
        [[ -e $intent || -L $intent ]] || continue
        prior=${intent##*/}
        prior=${prior%.intent}
        if deployment_effect_settled "$root" "$intent" \
            && cmp -s "$root/$prior/response.status" <(printf '0\n') \
            && cmp -s "$intent" <(printf '%s\0' "$prior" "$((grace + 5))" "$root/$prior" stop --time "$grace" "${deployment_ids[$role]}"); then
            return 0
        fi
    done
    return 1
}

deployment_fence_receipts() {
    local identifier=$1 phase receipt
    for phase in "${!deployment_pending_ids[@]}"; do
        [[ ${deployment_pending_ids[$phase]} == "$identifier" ]] || continue
        receipt=$deployment_runtime_root/$phase
        (set -o noclobber; printf 'fenced\n' > "$receipt/reconciled") || return $?
        sync -f -- "$receipt/reconciled" "$receipt" || return $?
        unset "deployment_pending_ids[$phase]"
    done
}

deployment_runtime_absent() {
    local role=$1 identifier=$2 phase=absent-$1-$2
    deployment_command "$phase" 10 "$operation_root/$phase" false \
        ps --all --no-trunc --quiet --filter "id=$identifier" || return $?
    [[ ! -s $operation_root/$phase/response.stdout ]] \
        || { deployment_refuse "$role original container is not confirmed absent; its late requests remain unfenced."; return 2; }
}

deployment_fence_role() {
    local role=$1 identifier=${deployment_ids[$1]} phase pending=false
    for phase in "${!deployment_pending_ids[@]}"; do
        if [[ ${deployment_pending_ids[$phase]} == "$identifier" ]]; then pending=true; break; fi
    done
    [[ $pending == true ]] || return 0
    printf 'Fencing late requests by removing stopped %s container %s; its bounded Docker logs will be discarded.\n' "$role" "$identifier" || return $?
    phase=${deployment_runtime_prefix}remove-$role
    local operation_root=$deployment_runtime_root
    deployment_command "$phase" 30 "$operation_root/$phase" true rm "$identifier" || return $?
    # Keep read observations with the replaceable observer, not persistent effects.
    operation_root=$deployment_runtime_root/runtime-observation
    deployment_runtime_absent "$role" "$identifier" || return $?
    deployment_fence_receipts "$identifier" || return $?
    unset "deployment_ids[$role]"
}

deployment_recover_runtime() {
    local root=$1 intent phase kind role identifier budget grace receipt opposing=false
    local deployment_runtime_root=$1 deployment_runtime_prefix=recover-$operation_generation-
    local deployment_runtime_fence=false deployment_resuming=false
    local -a fields=() expected=()
    local -A deployment_pending_ids=() deployment_pending_roles=() pending_kinds=() pending_graces=()
    [[ $root == "$operation_root" ]] || return 2
    for intent in "$root/effects/"*; do
        [[ -e $intent || -L $intent ]] || continue
        if deployment_effect_settled "$root" "$intent"; then continue; fi
        [[ -f $intent && ! -L $intent && $(stat -c '%u:%a' -- "$intent") == "$caller_uid:600" ]] \
            && (( $(stat -c '%s' -- "$intent") < 65536 )) \
            && cmp -s <(tail -c 1 -- "$intent") <(printf '\0') || return 2
        mapfile -d '' -t fields < "$intent"
        (( ${#fields[@]} >= 5 )) || return 2
        phase=${fields[0]} budget=${fields[1]} receipt=$root/${fields[0]}
        [[ $intent == "$root/effects/$phase.intent" && ${fields[2]} == "$receipt" ]] \
            && deployment_runtime_phase "$phase" || return 2
        kind=$deployment_runtime_kind role=$deployment_runtime_role
        identifier=${fields[${#fields[@]}-1]}
        [[ $identifier =~ ^[0-9a-f]{64}$ ]] || return 2
        case $kind in
            start) budget=30; expected=(start "$identifier") ;;
            stop)
                (( ${#fields[@]} == 7 )) || return 2
                grace=${fields[5]}; deployment_seconds "$grace" || return 2
                budget=$((grace + 5)); pending_graces[$phase]=$grace
                expected=(stop --time "$grace" "$identifier") ;;
            replace|remove) budget=30; expected=(rm "$identifier") ;;
        esac
        cmp -s "$intent" <(printf '%s\0' "$phase" "$budget" "$receipt" "${expected[@]}") || return 2
        deployment_pending_ids[$phase]=$identifier deployment_pending_roles[$phase]=$role pending_kinds[$phase]=$kind
        if [[ $kind == replace || $kind == remove || ( $kind == start && $deployment_action != up ) \
            || ( $kind == stop && $deployment_action != down ) ]]; then opposing=true; fi
    done
    (( ${#deployment_pending_ids[@]} )) || return 2
    # The parent retains persistent requests. Only this observer's fully retired
    # finite helpers permit replacing its previous read receipts.
    local operation_root=$root/runtime-observation
    if [[ -e $operation_root || -L $operation_root ]]; then
        deployment_private_directory "$operation_root" false || return $?
        deployment_effects_settled "$operation_root" || deployment_recover_helpers "$operation_root" || return 2
        deployment_remove_operation "$operation_root" || return $?
    fi
    mkdir -m 700 -- "$operation_root" "$operation_root/effects" || return $?
    deployment_bootstrap || return $?
    if (( deployment_existing_count == 0 )); then
        deployment_selected_bundle && deployment_load_bootstrap "$selected_bundle" || return $?
    fi
    deployment_inventory observation || return $?
    for phase in "${!deployment_pending_ids[@]}"; do
        [[ -v deployment_pending_ids[$phase] ]] || continue
        role=${deployment_pending_roles[$phase]} identifier=${deployment_pending_ids[$phase]}
        if [[ ! -v deployment_ids[$role] || ${deployment_ids[$role]} != "$identifier" ]]; then
            deployment_runtime_absent "$role" "$identifier" || return $?
            deployment_fence_receipts "$identifier" || return $?
        elif [[ ${pending_kinds[$phase]} == stop && ${pending_graces[$phase]} != "${deployment_stop_seconds[$role]}" ]]; then
            deployment_refuse "$role original stop grace does not match its admitted bundle; preserve the request."
            return 2
        fi
    done
    if (( ${#deployment_pending_ids[@]} == 0 )); then deployment_effects_settled "$root"; return $?; fi
    if [[ $deployment_action == install ]]; then
        # Down may leave a late-stop restriction after establishing stopped
        # ownership. Install can fence that ID, but cannot acquire down's power
        # to stop live work or substitute a stopped snapshot for an actual ack.
        for role in "${!deployment_ids[@]}"; do
            [[ ${deployment_running[$role]} == false && ${deployment_restarting[$role]} == false && ${deployment_pids[$role]} == 0 ]] \
                || { deployment_refuse 'install cannot stop running or transitional work; use down explicitly and retain the original receipts.'; return 2; }
        done
        for phase in "${!deployment_pending_ids[@]}"; do
            role=${deployment_pending_roles[$phase]}
            deployment_acknowledged_stop "$role" "${deployment_stop_seconds[$role]}" \
                || { deployment_refuse 'install requires an acknowledged stop before fencing a pending runtime target; use down explicitly and retain the original receipts.'; return 2; }
        done
        for role in provider prediction reader smiles api interpreter store; do
            [[ -v deployment_ids[$role] ]] || continue
            deployment_fence_role "$role" || return $?
        done
        deployment_effects_settled "$root"
        return $?
    fi
    if [[ $opposing == true ]]; then
        # Every pending target must be fenced when this shutdown opposes a start,
        # or a subsequent startup would oppose an earlier stop. Provider goes first.
        deployment_runtime_fence=true
        deployment_down || return $?
        deployment_effects_settled "$root"
    else
        if [[ $deployment_action == up ]]; then
            (( ${#deployment_ids[@]} == 7 )) \
                || { deployment_refuse 'interrupted startup has an incomplete admitted stack; use down to fence its original requests before restarting.'; return 2; }
            deployment_selected_bundle || return $?
            for role in "${!deployment_ids[@]}"; do
                [[ ${deployment_bundles[$role]} == "${selected_bundle##*/}" ]] \
                    || { deployment_refuse 'interrupted startup belongs to another bundle; use down to fence its original requests before replacement.'; return 2; }
            done
            deployment_resuming=true deployment_runtime_prefix=resume-$operation_generation-
            deployment_up || return $?
        else
            deployment_down || return $?
        fi
        deployment_recovery_observed=true
    fi
}
