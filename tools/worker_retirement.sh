#!/usr/bin/env bash
# Source-only engine-side retirement for one host-owned scientific allocation.
# Caller owns exclusive access to the private allocation directory, worker.cid
# admission at create, and the scrubbed docker_client.sh engine argv. A correctly
# spelled ID alone is not ownership. The worker must never mount this directory.
# Success proves stopped-container removal, not release of IPC grants or Attempt
# ownership. The allocation owner must confirm those separately before retire().
# Byte-exact CID admission and capped metadata receipts follow tools/docker.sh;
# inference_native.sh owns local client termination, never engine-side retirement.

worker_container_created() {
    # Admission is shared by native roles and foreground service components.
    # The caller retains create's exit status separately: a failed create may
    # yield an exact cleanup handle, but cannot authorize starting the container.
    local directory=$1 candidate receipt
    worker_created_id= worker_retirement_diagnostic=
    if [[ $inference_retirement_confirmed != true ]]; then
        worker_retirement_diagnostic='Cannot admit created container: its original engine client has not stopped; preserve its ownership and receipts.'
        return 2
    fi
    for receipt in "$directory/worker.cid" "$directory/create.stdout"; do
        if [[ ! -f $receipt || -L $receipt ]] || [[ $(stat -c '%u:%a' -- "$receipt") != "$(id -u):600" ]]; then
            worker_retirement_diagnostic='Cannot admit created container: require caller-owned private regular CID and complete response receipts.'
            return 2
        fi
    done
    if IFS= read -r -N 64 candidate < "$directory/worker.cid" && [[ $candidate =~ ^[0-9a-f]{64}$ ]] \
        && { cmp -s -- "$directory/worker.cid" <(printf '%s' "$candidate") \
            || cmp -s -- "$directory/worker.cid" <(printf '%s\n' "$candidate"); } \
        && cmp -s -- "$directory/create.stdout" <(printf '%s\n' "$candidate"); then
        worker_created_id=$candidate
        return 0
    fi
    worker_retirement_diagnostic='Cannot admit created container: complete response and private CID differ; preserve the original create and its receipts.'
    return 2
}

worker_container_retire() {
    local identifier=$1 allocation_directory=$2
    shift 2
    local receipts=$allocation_directory/retirement
    worker_retirement_diagnostic=
    if [[ $inference_retirement_confirmed != true ]]; then
        worker_retirement_diagnostic='Cannot retire worker: a previous engine client has not stopped; preserve its ownership and receipts.'
        return 2
    fi
    if [[ ! $identifier =~ ^[0-9a-f]{64}$ || ! -f $allocation_directory/worker.cid || -L $allocation_directory/worker.cid ]] \
        || { ! cmp -s -- "$allocation_directory/worker.cid" <(printf '%s' "$identifier") \
            && ! cmp -s -- "$allocation_directory/worker.cid" <(printf '%s\n' "$identifier"); }; then
        worker_retirement_diagnostic='Cannot retire worker: its exact container ID does not match the private allocation receipt.'
        return 2
    fi
    # Exclusive creation prevents retry from overwriting uncertain earlier work.
    # Reconciliation is an explicit owner operation, not an automatic retry here.
    if (umask 077; mkdir -- "$receipts") 2>/dev/null; then :; else
        worker_retirement_diagnostic='Cannot retire worker: a fresh private retirement receipt directory could not be created; preserve any earlier receipts.'
        return 2
    fi
    if worker_retirement_command stop "$receipts" "$@" stop --time 5 "$identifier"; then :; else return $?; fi
    if worker_retirement_command inspect "$receipts" "$@" inspect --type container \
        --format '{{.Id}} {{.State.Status}} {{.State.Running}} {{.State.Pid}} {{.State.Restarting}}' "$identifier"; then :; else return $?; fi
    # A failed start can leave an admitted container that has never run.
    if ! cmp -s -- "$receipts/inspect.stdout" <(printf '%s exited false 0 false\n' "$identifier") \
        && ! cmp -s -- "$receipts/inspect.stdout" <(printf '%s created false 0 false\n' "$identifier"); then
        printf -v worker_retirement_diagnostic 'Cannot confirm worker stopped: inspection did not report the exact container in the exited or never-started state with no process or restart. Private receipts: %q.' "$receipts"
        return 2
    fi
    # json-file logs disappear on removal. Deliver the stopped container's last
    # messages directly to the operator first, including a fatal traceback or a
    # failure during TERM. A broken sink retains this exact stopped owner.
    if inference_native 'stopped container diagnostics' 30 stdio "$@" logs --tail 80 "$identifier" >&2; then :; else
        local log_status=$?
        printf -v worker_retirement_diagnostic 'Cannot remove stopped container: bounded Docker log delivery failed (status %s, cancellation %s, local client retired %s); retain its original receipts. %s' \
            "$log_status" "$inference_cancellation_status" "$inference_retirement_confirmed" "$inference_diagnostic"
        return 2
    fi
    worker_retirement_command remove "$receipts" "$@" rm "$identifier"
}

worker_retirement_command() {
    local operation=$1 receipts=$2 status receipt size
    shift 2
    # Both files are bounded independently. Do not copy engine diagnostics to a
    # public channel: the allocation owner retains them for protected reporting.
    if inference_native "worker $operation" 10 stdio bash -eu -c '
        receipts=$1 operation=$2; shift 2
        umask 077
        ulimit -f 4
        set -o noclobber
        exec "$@" > "$receipts/$operation.stdout" 2> "$receipts/$operation.stderr"
    ' bash "$receipts" "$operation" "$@"; then status=0; else status=$?; fi
    worker_retirement_diagnostic+=$inference_diagnostic
    if [[ $status != 0 || $inference_retirement_confirmed != true || $inference_cancellation_status != 0 ]]; then
        printf -v worker_retirement_diagnostic '%sCannot complete worker lifecycle: %s did not complete cleanly (status %s, cancellation %s, local client retired %s). Preserve allocation and private receipts: %q.' \
            "$worker_retirement_diagnostic" "$operation" "$status" "$inference_cancellation_status" "$inference_retirement_confirmed" "$receipts"
        return 2
    fi
    # An engine can ignore a write error and still exit zero. A file at the
    # limit cannot establish complete diagnostic capture (tools/docker.sh).
    for receipt in "$receipts/$operation.stdout" "$receipts/$operation.stderr"; do
        if [[ -f $receipt && ! -L $receipt ]] && size=$(stat -c '%s' -- "$receipt" 2>/dev/null) \
            && [[ $size =~ ^(0|[1-9][0-9]{0,3})$ && $size -lt 4096 ]]; then continue; fi
        printf -v worker_retirement_diagnostic '%sCannot complete worker lifecycle: %s receipts are missing or may be incomplete at the capture limit. Preserve private receipts: %q.' \
            "$worker_retirement_diagnostic" "$operation" "$receipts"
        return 2
    done
}
