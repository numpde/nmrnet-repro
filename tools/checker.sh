#!/usr/bin/env bash
# Explicit dependency acquisition and offline, captured checker-image builds.
# Magnet separates dependency preparation from ordinary check execution:
# https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/Makefile#L387-L425
set -euo pipefail

readonly base='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
# Finite client deadlines follow dapp32's child-before-teardown proof:
# https://github.com/numpde/dapp32/blob/39819ec60d0b38169b0beb20f0742b308eb4fe74/tests/checks/test_release_approval_image.py#L342-L360
# Keep inference_native's TERM grace and held ownership; client retirement does
# not establish daemon-side container/build retirement. Offline builds share
# provider_image.sh/geometry_image.sh's 900-second operation budget.
readonly dependency_check_seconds=60 build_seconds=900
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly caller_uid="$(id -u)" caller_gid="$(id -g)"
print_command=false
if [[ $# -gt 0 && "$1" == --print ]]; then print_command=true; shift; fi
if [[ $# -ne 1 || ! "$1" =~ ^(download|check|build)$ ]]; then
    printf '%s\n' 'Usage: bash tools/checker.sh [--print] {download|check|build}' >&2
    exit 2
fi
if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
    printf '%s\n' 'Checker preparation requires a nonzero caller UID and GID.' >&2
    exit 2
fi
if [[ "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
    printf '%s\n' 'Checker preparation cannot use a repository path containing commas or line breaks.' >&2
    exit 2
fi
for directory in "$repository_root/.work" "$repository_root/.work/checker"; do
    if [[ -L "$directory" || ( -e "$directory" && ( ! -d "$directory" || "$(stat -c '%u' "$directory")" != "$caller_uid" ) ) ]]; then
        printf '%s\n' 'Checker preparation requires owned directories without symlinks.' >&2
        exit 2
    fi
done
for directory in tools requirements containers containers/checker; do
    if [[ ! -d "$repository_root/$directory" || -L "$repository_root/$directory" ]]; then
        printf '%s\n' 'Checker source inputs require regular parent directories without symlinks.' >&2
        exit 2
    fi
done
readonly declaration="$repository_root/requirements/checker-dependencies.json"
if [[ ! -f "$declaration" || -L "$declaration" ]]; then
    printf '%s\n' 'Checker preparation requires the reviewed requirements/checker-dependencies.json declaration.' >&2
    exit 2
fi
readonly dependencies="$repository_root/.work/checker/dependencies"
resume_arguments=()
if [[ ${NMRNET_CHECKER_RESUME_STAGE_INPUT_ORIGIN-} == 'command line' ]]; then
    resume_stage=${NMRNET_CHECKER_RESUME_STAGE_INPUT-}
    if [[ $1 != download || ! $resume_stage =~ ^\.dependencies\.pending-[[:alnum:]_]+$ ]]; then
        printf '%s\n' 'RESUME_STAGE must name one retained .dependencies.pending-... directory for checker download.' >&2
        exit 2
    fi
    resume_path="$repository_root/.work/checker/$resume_stage"
    if ! "$print_command" && [[ ! -d $resume_path || -L $resume_path || $(stat -c %u -- "$resume_path") != "$caller_uid" ]]; then
        printf '%s\n' 'Cannot resume checker dependencies: select a retained caller-owned directory without a symlink.' >&2
        exit 2
    fi
    resume_arguments=(--resume-stage "/output/$resume_stage")
fi
source "$repository_root/tools/wheelhouse_path.sh"
select_wheelhouse
if [[ "$1" == download ]]; then
    if ! "$print_command"; then
        mkdir -p -- "$wheelhouse_directory"
        pool_owner=$(stat -c '%u:%g' -- "$wheelhouse_directory")
        [[ $pool_owner == "$caller_uid:$caller_gid" ]] || {
            printf 'Checker acquisition requires pool owner %s:%s; observed %s.\n' "$caller_uid" "$caller_gid" "$pool_owner" >&2
            exit 2
        }
    fi
else
    require_wheelhouse_pool
    if ! "$print_command"; then lock_wheelhouse shared; fi
fi
if [[ $1 == build ]] && ! "$print_command"; then
    source "$repository_root/tools/image_lifecycle.sh"
    image_lifecycle_lock shared
fi
run() {
    if "$print_command"; then printf '%q ' "$@"; printf '\n'; else "$@"; fi
}
worker=(--pull never --read-only --user "$caller_uid:$caller_gid" --cap-drop ALL
    --security-opt no-new-privileges:true --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --env NMRNET_IN_CONTAINER=1 --workdir /workspace)
tool_mounts=(--mount "type=bind,src=$repository_root/tools/checker.py,dst=/workspace/tools/checker.py,readonly"
    --mount "type=bind,src=$repository_root/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
    --mount "type=bind,src=$repository_root/tools/wheelhouse.py,dst=/workspace/tools/wheelhouse.py,readonly"
    --mount "type=bind,src=$repository_root/tools/wheelhouse_acquire.py,dst=/workspace/tools/wheelhouse_acquire.py,readonly"
    --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
    --mount "type=bind,src=$declaration,dst=/input/dependencies.json,readonly"
    --mount "type=bind,src=$repository_root/requirements/wheelhouse-acquirer-assets.in,dst=/input/tool-assets.in,readonly"
    --mount "type=bind,src=$repository_root/requirements/wheelhouse-tool-assets.in,dst=/input/stream-assets.in,readonly")
if [[ "$1" == download ]]; then
    source "$repository_root/tools/acquisition_interface.sh"
    select_acquisition_interface
    if ! "$print_command"; then mkdir -p -m 700 -- "$repository_root/.work/checker"; fi
    proxy_command=(bash "$repository_root/tools/docker.sh")
    if "$print_command"; then proxy_command+=(--print); fi
    download_command=("${proxy_command[@]}" --success-note $'\nNext, verify the checker dependencies offline:\n  make checks/dependencies/check' --pool-owner "$caller_uid:$caller_gid" run "${worker[@]}" "${tool_mounts[@]}"
        --mount "type=bind,src=$repository_root/.work/checker,dst=/output"
        --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse"
        "$base" python3 -B -m tools.checker download --declaration /input/dependencies.json
        --directory /output/dependencies --base "$base" --wheelhouse /wheelhouse --tool-assets /input/tool-assets.in --stream-assets /input/stream-assets.in "${resume_arguments[@]}")
    # No checker client exists yet. Transfer the supported PID so a directed
    # signal reaches the acquisition owner and its cleanup, without a wrapper.
    exec "${download_command[@]}"
fi
if [[ ! -d "$dependencies" || -L "$dependencies" ]]; then
    printf '%s\n' 'Checker preparation requires acquired dependencies; run the explicit interface-bound download target first.' >&2
    exit 2
fi
source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
docker_client_directory=
capture=
temporary_record=
admission_directory=
inference_diagnostic=
inference_retirement_confirmed=true
inference_cancellation_status=0
caller_cancellation=0
capture_borrowed=false
next_actions=
cleanup() {
    local status=$? record_status=0 capture_status=0 client_status=0 admission_status=0 cancelled=0
    local native_cancellation=$inference_cancellation_status
    trap - EXIT
    if "$print_command"; then exit "$status"; fi
    trap 'cancelled=129' HUP
    trap 'cancelled=130' INT
    trap 'cancelled=143' TERM
    # One native fact gates every resource. Finish quiet removals before any
    # public reporting, as in inference_image.sh; cancellation starts no report.
    if "$inference_retirement_confirmed"; then
        if [[ -n "$temporary_record" ]]; then
            if rm -f -- "$temporary_record" 2>/dev/null; then temporary_record=; else
                record_status=$?
                if [[ $status == 0 ]]; then status=$record_status; fi
            fi
        fi
        if [[ -n "$capture" ]] && ! "$capture_borrowed"; then
            if rm -rf -- "$capture" 2>/dev/null; then capture=; else
                capture_status=$?
                if [[ $status == 0 ]]; then status=$capture_status; fi
            fi
        fi
        if docker_client_cleanup 2>/dev/null; then :; else
            client_status=$?
            if [[ $status == 0 ]]; then status=$client_status; fi
        fi
        # Failed workflows retain the capped daemon cause outside build capture.
        # Nonzero removal status distinguishes an attempted removal from keeping
        # evidence; successful removal clears the only owned path binding.
        if [[ $status == 0 && -n "$admission_directory" ]]; then
            if rm -rf -- "$admission_directory" 2>/dev/null; then admission_directory=; else
                admission_status=$?
                status=$admission_status
            fi
        fi
    fi
    trap 'caller_cancellation=129; exit 129' HUP
    trap 'caller_cancellation=130; exit 130' INT
    trap 'caller_cancellation=143; exit 143' TERM
    if [[ $cancelled != 0 ]]; then exit "$cancelled"; fi
    if [[ $caller_cancellation != 0 ]]; then exit "$caller_cancellation"; fi
    # Snapshot the previous owner's actual signal before reporting replaces its
    # outcome. Ordinary client exit 129/130/143 still needs its cause presented.
    if [[ $native_cancellation != 0 ]]; then exit "$native_cancellation"; fi
    if ! "$inference_retirement_confirmed"; then
        printf '%s' "$inference_diagnostic" >&2
        if [[ -n "$docker_client_directory" ]]; then
            printf 'Cannot confirm checker native client retirement; private Docker client state retained at %q.\n' "$docker_client_directory" >&2
        fi
        if [[ -n "$capture" ]]; then
            printf 'Cannot confirm checker native client retirement; build capture retained at %q.\n' "$capture" >&2
        fi
        if [[ -n "$temporary_record" ]]; then
            printf 'Cannot confirm checker native client retirement; unpublished image record retained at %q.\n' "$temporary_record" >&2
        fi
        if [[ -n "$admission_directory" ]]; then
            printf 'Cannot confirm checker native client retirement; private inspection receipts retained at %q.\n' "$admission_directory" >&2
        fi
    elif [[ -n "$inference_diagnostic$temporary_record$capture$docker_client_directory$admission_directory" ]]; then
        # Reporting replaces the native fact; no later cleanup uses it.
        if inference_native 'checker cleanup reporting' untimed stdio bash -eu -c '
            printf "%s" "$1" >&2
            if [[ -n "$2" ]]; then
                printf "Cannot confirm unpublished checker image record removal; attempted path %q (status %s).\n" "$2" "$3" >&2
            fi
            if [[ -n "$4" ]]; then
                if [[ "$5" == 0 ]]; then
                    printf "Checker operation retirement is unconfirmed; inputs retained at %q.\n" "$4" >&2
                else
                    printf "Cannot confirm checker build capture removal; attempted path %q (status %s).\n" "$4" "$5" >&2
                fi
            fi
            if [[ -n "$6" ]]; then
                printf "Cannot confirm checker Docker client configuration removal; attempted path %q (status %s).\n" "$6" "$7" >&2
            fi
            if [[ -n "$8" ]]; then
                if [[ "$9" == 0 ]]; then
                    printf "Checker image inspection receipts retained at %q.\n" "$8" >&2
                else
                    printf "Cannot confirm checker inspection receipt removal; attempted path %q (status %s).\n" "$8" "$9" >&2
                fi
            fi
        ' bash "$inference_diagnostic" "$temporary_record" "$record_status" "$capture" "$capture_status" \
            "$docker_client_directory" "$client_status" "$admission_directory" "$admission_status"; then :; else
            local report_status=$?
            if [[ $inference_cancellation_status != 0 ]]; then
                status=$inference_cancellation_status
            elif [[ $status == 0 ]]; then status=$report_status; fi
        fi
    fi
    if [[ $status == 0 && -n $next_actions ]]; then
        if inference_native 'next-action reporting' untimed stdio bash -eu -c \
            'printf "%s\n" "$1" >&2' bash "$next_actions"; then :; else status=$?; fi
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'caller_cancellation=129; exit 129' HUP
trap 'caller_cancellation=130; exit 130' INT
trap 'caller_cancellation=143; exit 143' TERM
if ! "$print_command"; then
    mapped_owner=$(bash "$repository_root/tools/docker.sh" --pool-owner "$caller_uid:$caller_gid" map-owner "$base")
    [[ $mapped_owner =~ ^[0-9]+:[0-9]+$ ]] || { printf 'Cannot map checker dependency owner: unexpected mapping output %q.\n' "$mapped_owner" >&2; exit 2; }
    worker[4]=$mapped_owner
else
    printf '%s\n' 'Checker worker UID:GID is resolved through the selected engine when executed.' >&2
    worker[4]='<mapped-owner>'
fi
if "$print_command"; then docker_client_prepare print || { printf '%s' "$docker_client_diagnostic" >&2; exit 2; }
elif docker_client_prepare run; then :
else
    preparation_status=$?
    inference_diagnostic+=$docker_client_diagnostic
    exit "$preparation_status"
fi
verify=("${engine[@]}" run --rm --network none "${worker[@]}" "${tool_mounts[@]}"
    --mount "type=bind,src=$dependencies,dst=/dependencies,readonly"
    --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly"
    "$base" python3 -B -m tools.checker check --declaration /input/dependencies.json
    --directory /dependencies --base "$base" --wheelhouse /wheelhouse --stream-assets /input/stream-assets.in)
if [[ "$1" == check ]]; then
    if "$print_command"; then run "${verify[@]}"; else
        inference_native 'checker dependency verification' "$dependency_check_seconds" stdio "${verify[@]}"
        next_actions=$'\nNext, build the repository checker image:\n  make checks/image/build'
    fi
    exit
fi
if ! "$print_command"; then
    inference_native 'checker dependency verification' "$dependency_check_seconds" stdio "${verify[@]}" >/dev/null
    mkdir -p -- "$repository_root/.work/checker"
    capture="$(mktemp -d "$repository_root/.work/checker/.build.XXXXXXXX")"
else
    capture="$repository_root/.work/checker/.build.PRINT"
fi
inputs=(containers/checker/Dockerfile containers/checker/Dockerfile.dockerignore
    tools/checker.py tools/checker.sh tools/artifacts.py tools/container_guard.py requirements/checker-dependencies.json
    requirements/wheelhouse-tool-assets.in)
if ! "$print_command"; then
    for input in "${inputs[@]}"; do
        [[ -f "$repository_root/$input" && ! -L "$repository_root/$input" ]] || {
            printf -v inference_diagnostic '%s\n' 'Checker build inputs must be regular files without symlinks.'; exit 2;
        }
        mkdir -p -- "$capture/context/$(dirname -- "$input")"
        cp --preserve=mode -- "$repository_root/$input" "$capture/context/$input"
    done
    mkdir -- "$capture/dependencies"
    cp -a -- "$dependencies"/*.deb "$dependencies/.complete" "$capture/dependencies/"
    captured_verify=("${engine[@]}" run --rm --network none "${worker[@]}")
    for tool in checker.py artifacts.py container_guard.py; do
        captured_verify+=(--mount "type=bind,src=$capture/context/tools/$tool,dst=/workspace/tools/$tool,readonly")
    done
    captured_verify+=(--mount "type=bind,src=$capture/context/requirements/checker-dependencies.json,dst=/input/dependencies.json,readonly"
        --mount "type=bind,src=$capture/dependencies,dst=/dependencies,readonly"
        --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly"
        "$base" python3 -B -m tools.checker check --declaration /input/dependencies.json
        --directory /dependencies --base "$base" --wheelhouse /wheelhouse)
    capture_borrowed=true
    inference_native 'checker captured dependency verification' "$dependency_check_seconds" stdio "${captured_verify[@]}" >/dev/null
    capture_borrowed=false
    input_id="$(cd -- "$capture/context"; { sha256sum -- "${inputs[@]}"; stat -c '%a %n' -- "${inputs[@]}"; } | sha256sum | cut -d ' ' -f1)"
else
    input_id=PRINT
fi
build=("${engine[@]}" build --builder default --quiet --network none --pull=false
    --label "io.nmrnet-repro.checker-inputs=$input_id"
    --file containers/checker/Dockerfile -)
if "$print_command"; then run "${build[@]}"; exit; fi
python3 -I -S - "$capture/context" "$capture" "${inputs[@]}" <<'PY'
import hashlib, json, pathlib, sys
context, output = map(pathlib.Path, sys.argv[1:3])
sources = []
for name in sys.argv[3:]:
    path = context / name
    sources.append({'path': str(path), 'destination': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'executable': bool(path.stat().st_mode & 0o111)})
declaration = json.loads((context / 'requirements/checker-dependencies.json').read_text())
for package in declaration['packages']:
    if package['name'] == 'ruff':
        continue
    path = output / 'dependencies' / package['file']
    sources.append({'path': str(path), 'destination': 'checker-dependencies/' + path.name,
                    'sha256': package['sha256'], 'executable': bool(path.stat().st_mode & 0o111)})
path = output / 'dependencies' / '.complete'
sources.append({'path': str(path), 'destination': 'checker-dependencies/.complete',
                'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'executable': bool(path.stat().st_mode & 0o111)})
(output / 'sources.json').write_text(json.dumps(sources))
wheel = next(package for package in declaration['packages'] if package['name'] == 'ruff')
(output / 'manifest.json').write_text(json.dumps([{'directory': 'checker-wheels', 'filename': wheel['file'], 'sha256': wheel['sha256']}]))
PY
tool_line=$(grep '^https://github.com/numpde/nmr-provider-tools/releases/download/' "$capture/context/requirements/wheelhouse-tool-assets.in")
tool_url=${tool_line%%#sha256=*}; tool_hash=${tool_line##*#sha256=}; tool_name=${tool_url##*/}
[[ $tool_name =~ ^wheelhouse-tool-[0-9]+\.[0-9]+\.[0-9]+\.pyz$ && $tool_hash =~ ^[0-9a-f]{64}$ ]] || exit 2
[[ -f $wheelhouse_directory/$tool_name && ! -L $wheelhouse_directory/$tool_name ]] || {
    printf '%s\n' 'Checker stream tool must be a regular file in the selected wheelhouse.' >&2; exit 2;
}
observed=$(sha256sum -- "$wheelhouse_directory/$tool_name")
[[ ${observed%% *} == "$tool_hash" ]] || { printf '%s\n' 'Checker stream tool differs from its pin.' >&2; exit 2; }
capture_borrowed=true
if inference_native 'checker image build' "$build_seconds" image-id bash -o pipefail -c '
    tool=$1 pool=$2 sources=$3 manifest=$4; shift 4
    python3 -I -S "$tool" stream --pool "$pool" --source-allowlist "$sources" --manifest "$manifest" | "$@"
' bash "$wheelhouse_directory/$tool_name" "$wheelhouse_directory" "$capture/sources.json" "$capture/manifest.json" "${build[@]}"; then
    build_status=0
else
    build_status=$?
fi
# The streamed sources are read by the joined local pipeline, not by a
# separate local build context. A failed pre-build bind verification is separate.
if "$inference_retirement_confirmed"; then capture_borrowed=false; fi
(( build_status == 0 )) || exit "$build_status"
image_id=$inference_result
admission_directory=$(umask 077; mktemp -d /tmp/nmrnet-checker-admission.XXXXXXXX)
(umask 077; : > "$admission_directory/stdout"; : > "$admission_directory/stderr")
inspect=("${engine[@]}" image inspect --format '{{.Id}} {{index .Config.Labels "io.nmrnet-repro.checker-inputs"}}' "$image_id")
# Preserve this combined ID/label receipt as bytes, as in baseline.sh admission.
# The wrapper only caps/redirects; the unchanged native owner holds its lifetime.
if inference_native 'checker image admission' 30 stdio bash -eu -c '
    directory=$1; shift
    umask 077
    ulimit -f 4
    exec "$@" > "$directory/stdout" 2> "$directory/stderr"
' bash "$admission_directory" "${inspect[@]}"; then :; else
    inspect_status=$?
    printf -v inference_diagnostic '%sCannot record checker image: local image inspection failed (status %s).\n' "$inference_diagnostic" "$inspect_status"
    exit "$inspect_status"
fi
if [[ ! -f "$admission_directory/stdout" || -L "$admission_directory/stdout" \
      || ! -f "$admission_directory/stderr" || -L "$admission_directory/stderr" ]] \
    || ! observed_size=$(stat -c '%s' -- "$admission_directory/stdout") \
    || ! diagnostic_size=$(stat -c '%s' -- "$admission_directory/stderr"); then
    printf -v inference_diagnostic '%s\n' 'Cannot record checker image: inspection receipts must be regular files without symlinks.'
    exit 2
fi
if [[ $observed_size -ge 4096 || $diagnostic_size -ge 4096 ]]; then
    printf -v inference_diagnostic '%s\n' 'Cannot record checker image: an inspection receipt reached the private 4096-byte limit.'
    exit 2
fi
if ! cmp -s -- "$admission_directory/stdout" <(printf '%s %s\n' "$image_id" "$input_id"); then
    printf -v inference_diagnostic '%s\n' 'Cannot record checker image: observed identity or captured-input label differs.'
    exit 2
fi
record="$repository_root/.work/checker/image-id"
if [[ -L "$record" || ( -e "$record" && ! -f "$record" ) ]]; then
    printf -v inference_diagnostic '%s\n' 'Cannot record checker image: existing image record is not a regular file.'
    exit 2
fi
temporary_record="$(mktemp "$repository_root/.work/checker/.image-id.XXXXXXXX")"
printf '%s\n' "$image_id" > "$temporary_record"
mv -T -- "$temporary_record" "$record"
temporary_record=
inference_native 'checker image reporting' untimed stdio bash -eu -c '
    printf "Checker image recorded: %s\n" "$1"
' bash "$image_id"
next_actions=$'\nThe repository checker is ready. To check repository conventions:\n  make test/repository'
