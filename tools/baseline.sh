#!/usr/bin/env bash
# Host authority for tools/baseline_prepare.py and tools/baseline_run.py only.
# tools/baseline_model.py owns the argv for NMRNetplusplus/uninmr/infer.py at
# upstream 3ba8b9c2f3d0a390d00685d238618345c6ab24d7; this launcher owns no model choices.
# Offline flags and immutable image records follow local tools/inference_image.sh.
set -euo pipefail

readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly caller_uid="$(id -u)"
readonly caller_gid="$(id -g)"
print_command=false
if [[ "${1:-}" == --print ]]; then print_command=true; shift; fi
if [[ $# != 1 || ! "$1" =~ ^(prepare|run)$ ]]; then
    printf '%s\n' 'Usage: bash tools/baseline.sh [--print] {prepare|run}' >&2
    exit 2
fi
action="$1"
if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
    printf '%s\n' 'Baseline lanes require a nonzero host UID and GID.' >&2
    exit 2
fi
if [[ "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
    printf '%s\n' 'Repository path cannot contain commas or line breaks in Docker bind mounts.' >&2
    exit 2
fi
engine=(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH
    docker --host unix:///var/run/docker.sock)
source "$repository_root/tools/inference_native.sh"
admission_directory=
admission_removing=false
admission_removal_started=false
baseline_cancelled=0
baseline_exit() {
    local status=$?
    trap - EXIT HUP INT TERM
    if [[ $baseline_cancelled != 0 ]]; then status=$baseline_cancelled; fi
    if [[ -n "$admission_directory" ]]; then
        if "$admission_removal_started"; then
            # Interrupted/failed rm can have partial effects. Its former path
            # does not establish that a directory or its receipts remain.
            printf 'Cannot confirm baseline image admission scratch removal; attempted path %q (status %s).\n' "$admission_directory" "$status" >&2
        else
            # A failed native owner can have unknown retirement. Retain these
            # receipts on every admission failure; do not infer from diagnostics.
            printf 'Baseline image admission did not complete; private receipts retained at %q.\n' "$admission_directory" >&2
        fi
    fi
    exit "$status"
}
baseline_interrupt() {
    if "$admission_removing"; then baseline_cancelled=$1; else exit "$1"; fi
}
trap baseline_exit EXIT
trap 'baseline_interrupt 129' HUP
trap 'baseline_interrupt 130' INT
trap 'baseline_interrupt 143' TERM
directories=(tools artifacts requirements .work .work/inference .work/prepared)
modules=(container_guard artifacts baseline_prepare)
declarations=(release.json)
inputs=(.work/inference/image-id requirements/inference.lock requirements/unicore-source.json)
if [[ "$action" == prepare ]]; then
    directories+=(.work/prepared/data .work/prepared/weight)
    declarations+=(data.selection.json weight.selection.json)
    inputs+=(.work/prepared/data/provenance.json .work/prepared/weight/provenance.json)
    output_parent="$repository_root/.work/baseline"
    output_name=prepared
    memory=2g
else
    # Makefile admits only command-line choices; tools/baseline_run.py validates
    # finite nonnegative tolerances before creating its exclusive output directory.
    for name in LABEL_ATOL ATOL RTOL RUN_ID; do
        variable="NMRNET_${name}_INPUT"
        if [[ -z "${!variable:-}" ]]; then
            printf 'Cannot run baseline: supply %s explicitly on the make command line.\n' "$name" >&2
            exit 2
        fi
    done
    if [[ ! "$NMRNET_RUN_ID_INPUT" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
        printf '%s\n' 'Cannot run baseline: RUN_ID must be 1-80 letters, digits, dots, underscores or hyphens, starting with a letter or digit.' >&2
        exit 2
    fi
    directories+=(.work/baseline .work/baseline/prepared .work/prepared/checkpoints)
    modules+=(baseline_model baseline_output baseline_result baseline_run)
    declarations+=(checkpoints.selection.json)
    inputs+=(.work/baseline/prepared/provenance.json .work/prepared/checkpoints/provenance.json)
    output_parent="$repository_root/.work/baseline/runs"
    output_name="$NMRNET_RUN_ID_INPUT"
    memory=4g
fi
if [[ -L "$output_parent" || ( -e "$output_parent" && ! -d "$output_parent" ) ]]; then
    printf 'Cannot %s baseline: output parent must be a directory without a symlink.\n' "$action" >&2
    exit 2
fi
for name in "${modules[@]}"; do inputs+=("tools/$name.py"); done
for name in "${declarations[@]}"; do inputs+=("artifacts/$name"); done
for name in "${directories[@]}"; do
    if [[ -L "$repository_root/$name" || ! -d "$repository_root/$name" ]]; then
        printf 'Cannot %s baseline: %s must be an existing directory without a symlink.\n' "$action" "$name" >&2
        exit 2
    fi
done
for name in "${inputs[@]}"; do
    if [[ -L "$repository_root/$name" || ! -f "$repository_root/$name" ]]; then
        printf 'Cannot %s baseline: %s must be a regular file without a symlink.\n' "$action" "$name" >&2
        exit 2
    fi
done
image_record="$repository_root/.work/inference/image-id"
# Admit bytes before using a shell value: Bash strips NULs and trailing LFs.
# The record format follows tools/inference_image.sh's immutable ID receipt.
if [[ "$(stat -c '%s' -- "$image_record")" != 72 ]] \
    || ! IFS= read -r image_id < "$image_record" \
    || [[ ! "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] \
    || ! cmp -s -- "$image_record" <(printf '%s\n' "$image_id"); then
    printf 'Cannot %s baseline: image-id does not contain an immutable image ID.\n' "$action" >&2
    exit 2
fi
if [[ -e "$output_parent/$output_name" || -L "$output_parent/$output_name" ]]; then
    printf 'Cannot %s baseline: output %s already exists; the existing result is preserved.\n' "$action" "$output_name" >&2
    exit 2
fi
repository_revision="$(git -C "$repository_root" rev-parse --verify HEAD)"
if [[ ! "$repository_revision" =~ ^[0-9a-f]{40}$ ]]; then
    printf 'Cannot %s baseline: repository HEAD must be a full Git commit ID.\n' "$action" >&2
    exit 2
fi
if [[ "$action" == run ]]; then
    # containers/inference/Dockerfile records the indexed source revision at build.
    # Inspect the same immutable image ID subsequently passed to Docker run.
    read -r mode revision stage path < <(git -C "$repository_root" ls-files --stage -- NMRNetplusplus)
    if [[ "$mode" != 160000 || ! "$revision" =~ ^[0-9a-f]{40}$ || "$stage" != 0 ]]; then
        printf '%s\n' 'Cannot run baseline: the recorded submodule revision is missing or unresolved.' >&2
        exit 2
    fi
    inspect=("${engine[@]}" image inspect --format '{{.Id}}|{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}' "$image_id")
else
    inspect=("${engine[@]}" image inspect --format '{{.Id}}' "$image_id")
fi
if "$print_command"; then
    printf '%q ' "${inspect[@]}"; printf '\n'
else
    admission_directory=$(umask 077; mktemp -d /tmp/nmrnet-baseline-admission.XXXXXXXX)
    (umask 077; : > "$admission_directory/stdout"; : > "$admission_directory/stderr")
    # One inspected immutable identity and labels follow Magnet's resolver:
    # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/resolve-image-identity.sh#L50-L83
    # Separate private 4-KiB receipts follow tools/container.sh. The existing
    # inference owner supplies its 30s metadata budget/1s TERM escalation;
    # this wrapper only redirects and caps files, never owns another supervisor.
    if inference_native 'baseline image admission' 30 discard bash -eu -c '
        directory=$1; shift
        umask 077
        ulimit -f 4
        exec "$@" > "$directory/stdout" 2> "$directory/stderr"
    ' bash "$admission_directory" "${inspect[@]}"; then :; else
        inspect_status=$?
        exit "$inspect_status"
    fi
    if [[ ! -f "$admission_directory/stdout" || -L "$admission_directory/stdout" \
        || ! -f "$admission_directory/stderr" || -L "$admission_directory/stderr" ]] \
        || ! observed_size=$(stat -c '%s' -- "$admission_directory/stdout") \
        || ! diagnostic_size=$(stat -c '%s' -- "$admission_directory/stderr"); then
        printf '%s\n' 'Cannot admit baseline image: inspection receipts must be regular files without symlinks.' >&2
        exit 2
    fi
    # A zero-status client can write exactly its file limit. Reject either
    # full receipt before identity admission, including ignored stderr bytes.
    if [[ $observed_size -ge 4096 || $diagnostic_size -ge 4096 ]]; then
        printf '%s\n' 'Cannot admit baseline image: an inspection receipt reached the private 4096-byte limit.' >&2
        exit 2
    fi
    if [[ "$action" == run ]]; then
        if ! cmp -s -- "$admission_directory/stdout" <(printf '%s|%s\n' "$image_id" "$revision"); then
            printf '%s\n' 'Cannot run baseline: observed inference image identity or upstream revision differs from the recorded inputs.' >&2
            exit 2
        fi
    elif ! cmp -s -- "$admission_directory/stdout" <(printf '%s\n' "$image_id"); then
        printf '%s\n' 'Cannot prepare baseline: observed inference image identity differs from image-id.' >&2
        exit 2
    fi
    admission_removal_started=true
    admission_removing=true
    if rm -rf -- "$admission_directory"; then :; else
        removal_status=$?
        exit "$removal_status"
    fi
    # Once rm returns, new signals must exit immediately. Leaving deferral on
    # past the queued-cancellation check could silently release scientific work.
    admission_removing=false
    if [[ $baseline_cancelled != 0 ]]; then exit "$baseline_cancelled"; fi
    admission_directory=
fi
if ! "$print_command"; then mkdir -p -- "$output_parent"; fi
command=("${engine[@]}" run --rm --network none --pull never --read-only
    --env NMRNET_IN_CONTAINER=1
    --user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 2 --memory "$memory" --memory-swap "$memory" --pids-limit 64
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m --workdir /workspace)
identity_arguments=(--image-id "$image_id" --repository-revision "$repository_revision")
for name in "${modules[@]}"; do
    command+=(--mount "type=bind,src=$repository_root/tools/$name.py,dst=/workspace/tools/$name.py,readonly")
    identity_arguments+=(--tool-source "tools/$name.py")
done
for name in "${declarations[@]}"; do
    command+=(--mount "type=bind,src=$repository_root/artifacts/$name,dst=/input/$name,readonly")
done
for name in inference.lock unicore-source.json; do
    command+=(--mount "type=bind,src=$repository_root/requirements/$name,dst=/input/$name,readonly")
done
# containers/inference/Dockerfile installs both declarations into /opt/inference.
# Admit their exact checkout bytes before either module can import scientific code.
verify_and_run='from pathlib import Path
import runpy
import sys
for name in ("inference.lock", "unicore-source.json"):
    if Path("/opt/inference/" + name).read_bytes() != Path("/input/" + name).read_bytes():
        raise ValueError("Cannot execute baseline: embedded " + name + " differs from checkout; rebuild the inference image from the current declarations")
runpy.run_module(sys.argv.pop(1), run_name="__main__")'
if [[ "$action" == prepare ]]; then
    for name in data weight; do
        command+=(--mount "type=bind,src=$repository_root/.work/prepared/$name,dst=/prepared/$name,readonly")
    done
    command+=(--mount "type=bind,src=$output_parent,dst=/baseline"
        "$image_id" python3 -B -c "$verify_and_run" tools.baseline_prepare
        --prepared-root /prepared --declaration /input/release.json
        --data-selection /input/data.selection.json --dictionary-selection /input/weight.selection.json
        --output /baseline/prepared --count 2 --max-atoms 128 --max-tokens 130)
else
    command+=(--mount "type=bind,src=$repository_root/.work/baseline/prepared,dst=/prepared,readonly"
        --mount "type=bind,src=$repository_root/.work/prepared/checkpoints,dst=/checkpoints,readonly"
        --mount "type=bind,src=$output_parent,dst=/runs"
        "$image_id" python3 -B -c "$verify_and_run" tools.baseline_run
        --prepared /prepared --checkpoint-root /checkpoints --declaration /input/release.json
        --selection /input/checkpoints.selection.json --lock /input/inference.lock --source /input/unicore-source.json
        --output "/runs/$output_name" --user-dir /opt/nmrnet/uninmr
        --upstream-revision "$revision"
        --label-atol "$NMRNET_LABEL_ATOL_INPUT" --atol "$NMRNET_ATOL_INPUT" --rtol "$NMRNET_RTOL_INPUT")
fi
command+=("${identity_arguments[@]}")
if "$print_command"; then
    printf '%q ' "${command[@]}"; printf '\n'
else
    exec "${command[@]}"
fi
