#!/usr/bin/env bash
# Engine-client deadlines do not establish cancellation of engine-side work.
# Capped private evidence follows Magnet's selected-image-build command policy:
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/selected-image-build.py
inference_native() {
    local operation=$1 budget=$2 result=$3
    shift 3
    local scratch leader drain status=0 cancelled=0 client_status=0 drain_status=0 reason
    inference_result=
    scratch=$(umask 077; mktemp -d /tmp/nmrnet-inference-native.XXXXXXXX)
    mkfifo -- "$scratch/output"
    trap 'cancelled=130' INT
    trap 'cancelled=143' TERM
    (
        umask 077
        local retained_status=0
        head -c 8388608 > "$scratch/command.log" || retained_status=$?
        cat >/dev/null || {
            local discard_status=$?
            if [[ $retained_status == 0 ]]; then retained_status=$discard_status; fi
        }
        exit "$retained_status"
    ) < "$scratch/output" &
    drain=$!
    # The monitor stays alive after client exit. Retire its process group before
    # joining it, so the group identity cannot be reused after an early exit.
    timeout --signal=TERM --kill-after=1 "$budget" bash -c '
        receipt=$1; shift
        # Deadline TERM records expiry without retiring the ownership anchor.
        # A late successful client cannot turn this expired budget into success.
        trap '\''if [[ ! -f "$receipt" ]]; then printf "%s\n" expired > "$receipt.deadline"; fi'\'' TERM
        (trap - TERM; exec "$@") </dev/null &
        child=$!
        wait "$child"
        status=$?
        printf "%s\n" "$status" > "$receipt.pending"
        mv -- "$receipt.pending" "$receipt"
        while :; do sleep 60; done
    ' bash "$scratch/status" "$@" > "$scratch/output" 2>&1 &
    leader=$!
    while [[ ! -f "$scratch/status" && $cancelled == 0 ]] && kill -0 "$leader" 2>/dev/null; do
        sleep 0.01
    done
    if [[ -f "$scratch/status" ]]; then read -r status < "$scratch/status"; fi
    if kill -0 "$leader" 2>/dev/null; then kill -KILL -- "-$leader" 2>/dev/null || :; fi
    wait "$leader" 2>/dev/null || client_status=$?
    wait "$drain" || drain_status=$?
    trap 'exit 130' INT
    trap 'exit 143' TERM
    reason="native client exited with status $status"
    if [[ ! -f "$scratch/status" ]]; then
        status=$client_status
        if [[ $status == 124 ]]; then reason='native client deadline elapsed'; else
            reason="native monitor ended before reporting client completion (status $status)"
        fi
    fi
    if [[ -f "$scratch/status.deadline" ]]; then
        status=124
        reason='native client deadline elapsed'
    fi
    if [[ $status == 0 && $drain_status != 0 ]]; then
        status=$drain_status
        reason='native output could not be retained or drained'
    fi
    if [[ $cancelled != 0 ]]; then
        status=$cancelled
        reason="native client was interrupted (status $status)"
    fi
    if [[ $status == 0 && $result == image-id ]]; then
        if [[ $(wc -c < "$scratch/command.log") != 72 ]] \
            || ! LC_ALL=C grep -a -x -E 'sha256:[0-9a-f]{64}' "$scratch/command.log" >/dev/null; then
            status=2
            reason='native identity output did not contain exactly one full immutable image ID'
        else
            read -r inference_result < "$scratch/command.log"
        fi
    fi
    if [[ $status != 0 ]]; then
        if rm -f -- "$scratch/output" "$scratch/status" "$scratch/status.pending" "$scratch/status.deadline"; then :; else
            printf 'Cannot remove inference client control files; retained directory %q.\n' "$scratch" >&2
        fi
        printf 'Cannot complete inference %s: %s; private capped output retained at %q.\n' \
            "$operation" "$reason" "$scratch/command.log" >&2
        return "$status"
    fi
    rm -rf -- "$scratch"
}
