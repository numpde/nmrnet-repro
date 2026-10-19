#!/usr/bin/env bash
# Fixed worker-retirement and scientific lifecycle probes; not a provider service.
# Qualification plan and evidence: notes/017_provider_core_plan_20261018.txt (P4/P5).
# Host owns Docker. The two contained probes receive disjoint control grants.
# Create admission follows tools/docker.sh at e845c5a; engine retirement delegates
# to worker_retirement.sh. No name, connection close or timeout proves retirement.
set -euo pipefail
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly base_image='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
readonly caller_uid="$(id -u)" caller_gid="$(id -g)"
if [[ $# != 1 || ! $1 =~ ^(success|grant-failure|unstarted|scientific|geometry-success|geometry-timeout)$ ]]; then
    printf '%s\n' 'Use make test/integration/provider-worker or test/integration/provider-scientific-worker for the fixed scenarios.' >&2
    exit 2
fi
readonly scenario=$1
if [[ $caller_uid == 0 || $caller_gid == 0 || $repository_root == *','* || $repository_root == *$'\n'* || $repository_root == *$'\r'* ]]; then
    printf '%s\n' 'Worker qualification requires a nonzero UID/GID and a repository path without commas or line breaks.' >&2
    exit 2
fi
source_directories=(tests tests/integration tests/integration/provider_worker tools nmrnet_provider)
source_files=(tests/integration/provider_worker/retirement_probe.py tools/container_guard.py nmrnet_provider/retirement.py nmrnet_provider/worker_protocol.py
    tools/worker_qualification.sh tools/worker_retirement.sh tools/inference_native.sh tools/docker_client.sh Makefile)
if [[ $scenario == geometry-* ]]; then
    source_directories+=(.work .work/checker nmrnet_inference tests/fixtures tests/fixtures/preparation tests/fixtures/preparation/normative)
    source_files+=(.work/checker/image-id tests/integration/provider_worker/geometry_probe.py
        nmrnet_provider/geometry_client.py nmrnet_provider/geometry_protocol.py nmrnet_provider/geometry_worker.py
        nmrnet_provider/__init__.py nmrnet_provider/input_geometry.py nmrnet_provider/xyz.py nmrnet_provider/worker_client.py nmrnet_inference/predict.py
        tests/fixtures/preparation/normative/mixed_records.xyz tests/fixtures/preparation/expected.json)
fi
if [[ $scenario == scientific || $scenario == geometry-* ]]; then
    if [[ -L $repository_root/.work/qualification || ( -e $repository_root/.work/qualification && ! -d $repository_root/.work/qualification ) ]]; then
        printf '%s\n' 'Cannot preserve worker qualification: .work/qualification must be a directory without a symlink.' >&2
        exit 2
    fi
fi
if [[ $scenario == scientific ]]; then
    source_directories+=(tests/integration/provider_core nmrnet_inference packages packages/provider-core
        artifacts requirements .work .work/inference .work/prepared .work/prepared/weight .work/prepared/checkpoints
        .work/wheelhouse .work/wheelhouse/cpu .work/provider-core .work/provider-core/wheelhouse)
    source_files+=(tests/integration/provider_worker/scientific_probe.py tools/provider_core_tests.py
        tools/artifacts.py tools/baseline_prepare.py
        artifacts/prediction-models.json requirements/inference.lock requirements/unicore-source.json
        requirements/provider-core-build.lock requirements/provider-core.lock .work/inference/image-id
        .work/wheelhouse/cpu/setuptools-84.0.0-py3-none-any.whl)
fi
for path in "${source_directories[@]}"; do
    if [[ ! -d $repository_root/$path || -L $repository_root/$path ]]; then
        printf 'Cannot qualify worker retirement: %s must be a source directory without a symlink.\n' "$path" >&2
        exit 2
    fi
done
for path in "${source_files[@]}"; do
    if [[ ! -f $repository_root/$path || -L $repository_root/$path ]]; then
        printf 'Cannot qualify worker retirement: %s must be a regular source file without a symlink.\n' "$path" >&2
        exit 2
    fi
done
source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
source "$repository_root/tools/worker_retirement.sh"
docker_client_directory=
inference_retirement_confirmed=true
inference_cancellation_status=0
inference_diagnostic=
worker_retirement_diagnostic=
pending_diagnostic=
scratch=
worker_id= controller_id=
worker_attempted=false controller_attempted=false
worker_removed=false controller_removed=false
unresolved_create=false
finished=false

retire_endpoint() {
    local role=$1 identifier=$2 status
    printf -v "${role}_attempted" '%s' true
    if worker_container_retire "$identifier" "$scratch/$role" "${engine[@]}"; then
        printf -v "${role}_removed" '%s' true
        return 0
    else status=$?; fi
    pending_diagnostic+=$worker_retirement_diagnostic
    return "$status"
}

finish() {
    local status=$? cleanup_status=0
    trap - EXIT
    # Do not re-enter an uncertain retirement or replace its native-owner fact.
    if [[ $inference_retirement_confirmed == true && -n $worker_id && $worker_attempted == false ]]; then
        retire_endpoint worker "$worker_id" || cleanup_status=$?
    fi
    if [[ $inference_retirement_confirmed == true && -n $controller_id && $controller_attempted == false ]]; then
        retire_endpoint controller "$controller_id" || cleanup_status=$?
    fi
    if [[ $status == 0 && $cleanup_status != 0 ]]; then status=$cleanup_status; fi
    if [[ $finished == true && $worker_removed == true && ( -z $controller_id || $controller_removed == true ) && $inference_retirement_confirmed == true ]]; then
        if rm -rf -- "$scratch"; then :; else
            cleanup_status=$?
            if [[ $status == 0 ]]; then status=$cleanup_status; fi
            printf -v pending_diagnostic '%sCannot confirm qualification scratch removal at %q (status %s).\n' "$pending_diagnostic" "$scratch" "$cleanup_status"
        fi
    elif [[ -n $scratch ]]; then
        printf -v pending_diagnostic '%sWorker qualification did not finish; preserve private allocation receipts at %q (unresolved create: %s).\n' \
            "$pending_diagnostic" "$scratch" "$unresolved_create"
        if [[ $status == 0 ]]; then status=2; fi
    fi
    if [[ $inference_retirement_confirmed == true ]]; then
        if docker_client_cleanup; then :; else
            cleanup_status=$?
            if [[ $status == 0 ]]; then status=$cleanup_status; fi
        fi
    fi
    if [[ -n $pending_diagnostic ]]; then printf '%s\n' "$pending_diagnostic" >&2; fi
    if [[ $status == 0 && $finished == true ]]; then
        printf 'Worker qualification passed: %s.\n' "$scenario"
        if [[ $scenario == scientific ]]; then printf 'Private qualification summary: %q\n' "$qualification_receipt/summary.json"; fi
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

metadata() {
    local operation=$1 directory=$2 status
    shift 2
    if worker_retirement_command "$operation" "$directory" "${engine[@]}" "$@"; then return 0; else status=$?; fi
    pending_diagnostic+=$worker_retirement_diagnostic
    return "$status"
}

start_endpoint() {
    local role=$1 candidate= create_status=0
    local endpoint_image=$image_id memory=128m file_limit=64 cpus=1 pids=32 scratch_size=8m
    local -a endpoint=(python3 -B /workspace/probe.py "$role" "$allocation" "$scenario")
    local -a grants=(--mount "type=bind,src=$scratch/$role/diagnostics,dst=/diagnostics")
    if [[ $role == worker ]]; then
        grants+=(--mount "type=bind,src=$scratch/ipc,dst=/ipc")
    else
        grants+=(--mount "type=bind,src=$scratch/ipc,dst=/ipc,readonly"
            --mount "type=bind,src=$scratch/request,dst=/control/request"
            --mount "type=bind,src=$scratch/receipt,dst=/control/receipt,readonly")
    fi
    if [[ $scenario == scientific ]]; then
        cpus=2 pids=64 scratch_size=64m
        grants+=(--mount "type=bind,src=$repository_root/artifacts/prediction-models.json,dst=/input/prediction-models.json,readonly")
        if [[ $role == worker ]]; then
            endpoint_image=$scientific_image_id memory=4g
            grants+=(--mount "type=bind,src=$repository_root/nmrnet_inference,dst=/workspace/nmrnet_inference,readonly"
                --mount "type=bind,src=$repository_root/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
                --mount "type=bind,src=$repository_root/tools/baseline_prepare.py,dst=/workspace/tools/baseline_prepare.py,readonly"
                --mount "type=bind,src=$repository_root/nmrnet_provider,dst=/workspace/nmrnet_provider,readonly"
                --mount "type=bind,src=$repository_root/tests/integration/provider_worker/scientific_probe.py,dst=/workspace/scientific_probe.py,readonly"
                --mount "type=bind,src=$repository_root/.work/prepared/weight,dst=/weight,readonly"
                --mount "type=bind,src=$repository_root/.work/prepared/checkpoints,dst=/checkpoints,readonly"
                --mount "type=bind,src=$repository_root/requirements/inference.lock,dst=/input/inference.lock,readonly"
                --mount "type=bind,src=$repository_root/requirements/unicore-source.json,dst=/input/unicore-source.json,readonly")
            # Match baseline.sh's embedded-declaration admission before importing
            # any decoder/model code. The image label was admitted by the host.
            endpoint=(python3 -B -c 'from pathlib import Path
import runpy
for name in ("inference.lock", "unicore-source.json"):
    if Path("/opt/inference/" + name).read_bytes() != Path("/input/" + name).read_bytes():
        raise ValueError("Cannot qualify scientific worker: embedded dependencies differ; rebuild the admitted inference image")
runpy.run_path("/workspace/scientific_probe.py", run_name="__main__")' "$allocation")
        else
            # The installed runtime needs executable mappings for cryptography;
            # no host executable output or model artifacts are granted here.
            memory=512m file_limit=65536
            grants+=(--env PROVIDER_CORE_IN_CONTAINER=1 --env NMRNET_LIVE_WORKER=1
                --env "NMRNET_QUALIFICATION_ALLOCATION=$allocation"
                --env "NMRNET_QUALIFICATION_IMAGE_ID=$scientific_image_id"
                --env "NMRNET_QUALIFICATION_CONTROLLER_IMAGE_ID=$image_id"
                --env "NMRNET_QUALIFICATION_REPOSITORY_REVISION=$repository_revision"
                --tmpfs /runtime:rw,nosuid,nodev,exec,size=128m
                --mount "type=bind,src=$scratch/controller/state,dst=/state"
                --mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                --mount "type=bind,src=$repository_root/packages/provider-core,dst=/package,readonly"
                --mount "type=bind,src=$repository_root/nmrnet_provider,dst=/consumer/nmrnet_provider,readonly"
                --mount "type=bind,src=$repository_root/nmrnet_inference,dst=/consumer/nmrnet_inference,readonly"
                --mount "type=bind,src=$repository_root/tools,dst=/consumer/tools,readonly"
                --mount "type=bind,src=$repository_root/tests/integration/provider_worker/scientific_probe.py,dst=/source/scientific_probe.py,readonly"
                --mount "type=bind,src=$repository_root/tests/integration/provider_core,dst=/consumer-tests,readonly"
                --mount "type=bind,src=$repository_root/requirements/provider-core-build.lock,dst=/core-build.lock,readonly"
                --mount "type=bind,src=$repository_root/requirements/provider-core.lock,dst=/core-runtime.lock,readonly"
                --mount "type=bind,src=$repository_root/.work/provider-core/wheelhouse,dst=/runtime-deps,readonly"
                --mount "type=bind,src=$repository_root/.work/wheelhouse/cpu/setuptools-84.0.0-py3-none-any.whl,dst=/build-deps/setuptools-84.0.0-py3-none-any.whl,readonly")
            endpoint=(python3 -B -m tools.provider_core_tests integration --pattern test_live_worker.py)
        fi
    fi
    if [[ $scenario == geometry-* ]]; then
        grants+=(--mount "type=bind,src=$repository_root/nmrnet_provider,dst=/workspace/nmrnet_provider,readonly"
            --mount "type=bind,src=$repository_root/nmrnet_inference/predict.py,dst=/workspace/nmrnet_inference/predict.py,readonly"
            --mount "type=bind,src=$repository_root/tests/integration/provider_worker/geometry_probe.py,dst=/workspace/geometry_probe.py,readonly")
        if [[ $role == controller ]]; then
            grants+=(--mount "type=bind,src=$repository_root/tests/fixtures/preparation/normative/mixed_records.xyz,dst=/input/mixed_records.xyz,readonly"
                --mount "type=bind,src=$repository_root/tests/fixtures/preparation/expected.json,dst=/input/expected.json,readonly")
        fi
        endpoint=(python3 -B /workspace/geometry_probe.py "$role" "$allocation" "$scenario")
    fi
    unresolved_create=true
    if metadata create "$scratch/$role" create --cidfile "$scratch/$role/worker.cid" \
        --init --network none --pull never --read-only --restart no --log-driver none \
        --user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus "$cpus" --memory "$memory" --memory-swap "$memory" --pids-limit "$pids" \
        --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=$scratch_size" --workdir /workspace \
        --env NMRNET_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 \
        --mount "type=bind,src=$repository_root/tests/integration/provider_worker/retirement_probe.py,dst=/workspace/probe.py,readonly" \
        --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly" \
        --mount "type=bind,src=$repository_root/nmrnet_provider/retirement.py,dst=/workspace/nmrnet_provider/retirement.py,readonly" \
        --mount "type=bind,src=$repository_root/nmrnet_provider/worker_protocol.py,dst=/workspace/nmrnet_provider/worker_protocol.py,readonly" \
        "${grants[@]}" "$endpoint_image" bash -eu -c \
        'limit=$1; shift; umask 077; ulimit -f "$limit"; exec "$@" > /diagnostics/stdout 2> /diagnostics/stderr' \
        bash "$file_limit" "${endpoint[@]}"; then :; else create_status=$?; fi
    # Even a failed client can leave a matching CID pair. Admit only complete
    # agreement; otherwise retain uncertainty without starting or deleting by name.
    if [[ $inference_retirement_confirmed == true && -f $scratch/$role/worker.cid && ! -L $scratch/$role/worker.cid ]] \
        && IFS= read -r -N 64 candidate < "$scratch/$role/worker.cid" \
        && [[ $candidate =~ ^[0-9a-f]{64}$ ]] \
        && { cmp -s -- "$scratch/$role/worker.cid" <(printf '%s' "$candidate") \
            || cmp -s -- "$scratch/$role/worker.cid" <(printf '%s\n' "$candidate"); } \
        && cmp -s -- "$scratch/$role/create.stdout" <(printf '%s\n' "$candidate"); then
        printf -v "${role}_id" '%s' "$candidate"
        unresolved_create=false
    else
        pending_diagnostic+='Cannot admit created probe container: private CID and complete create response do not agree. '
        return 2
    fi
    if [[ $create_status != 0 ]]; then return "$create_status"; fi
    if [[ $scenario == unstarted && $role == worker ]]; then return 0; fi
    metadata start "$scratch/$role" start "$candidate"
}

await_bytes() {
    local path=$1 expected=$2 seconds=$3 deadline
    deadline=$((SECONDS + seconds))
    while (( SECONDS < deadline )); do
        if [[ -f $path && ! -L $path ]] && cmp -s -- "$path" <(printf '%s\n' "$expected"); then return 0; fi
        sleep 0.05
    done
    pending_diagnostic+='Worker qualification timed out awaiting its exact allocation-bound observation. '
    return 2
}

scratch=$(umask 077; mktemp -d /tmp/nmrnet-worker-proof-XXXXXXXX)
allocation=${scratch##*/}
mkdir -m 700 -- "$scratch/worker" "$scratch/controller" "$scratch/ipc" "$scratch/request" "$scratch/receipt"
mkdir -m 700 -- "$scratch/worker/diagnostics" "$scratch/controller/diagnostics"
if [[ $scenario == geometry-* ]]; then
    (cd -- "$repository_root" && sha256sum -- "${source_files[@]}") > "$scratch/sources.sha256"
fi
if docker_client_prepare run; then :; else pending_diagnostic+=$docker_client_diagnostic; exit 2; fi
probe_image=$base_image
if [[ $scenario == geometry-* ]]; then
    if ! IFS= read -r probe_image < "$repository_root/.work/checker/image-id" \
        || [[ ! $probe_image =~ ^sha256:[0-9a-f]{64}$ ]] \
        || ! cmp -s -- "$repository_root/.work/checker/image-id" <(printf '%s\n' "$probe_image"); then
        pending_diagnostic+='Cannot admit geometry probe: .work/checker/image-id must record one immutable image ID and newline. '
        exit 2
    fi
fi
metadata image "$scratch" image inspect --format '{{.Id}}' "$probe_image"
IFS= read -r image_id < "$scratch/image.stdout"
if [[ ! $image_id =~ ^sha256:[0-9a-f]{64}$ ]] || ! cmp -s -- "$scratch/image.stdout" <(printf '%s\n' "$image_id"); then
    pending_diagnostic+='Cannot admit probe image: inspection did not return one immutable image ID. '
    exit 2
fi
if [[ $scenario == geometry-* && $image_id != "$probe_image" ]]; then
    pending_diagnostic+='Cannot admit geometry probe: inspected image differs from the recorded checker ID. '
    exit 2
fi
if [[ $scenario == scientific ]]; then
    source "$repository_root/tools/git_metadata.sh"
    repository_revision=$(repository_git -C "$repository_root" rev-parse --verify HEAD)
    read -r mode revision stage path < <(repository_git -C "$repository_root" ls-files --stage -- NMRNetplusplus)
    if [[ ! $repository_revision =~ ^[0-9a-f]{40}$ ]]; then
        pending_diagnostic+='Cannot admit scientific worker: repository HEAD must resolve to one full commit ID. '
        exit 2
    fi
    if [[ $mode != 160000 || ! $revision =~ ^[0-9a-f]{40}$ || $stage != 0 ]]; then
        pending_diagnostic+='Cannot admit scientific worker: NMRNetplusplus must have one unconflicted submodule commit in the Git index. '
        exit 2
    fi
    if ! IFS= read -r scientific_image_id < "$repository_root/.work/inference/image-id" \
        || [[ ! $scientific_image_id =~ ^sha256:[0-9a-f]{64}$ ]] \
        || ! cmp -s -- "$repository_root/.work/inference/image-id" <(printf '%s\n' "$scientific_image_id"); then
        pending_diagnostic+='Cannot admit scientific worker: .work/inference/image-id must contain one sha256 image ID followed by a newline; use make packages/image to record it. '
        exit 2
    fi
    metadata scientific-image "$scratch" image inspect --format '{{.Id}}|{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}' "$scientific_image_id"
    if ! cmp -s -- "$scratch/scientific-image.stdout" <(printf '%s|%s\n' "$scientific_image_id" "$revision"); then
        pending_diagnostic+='Cannot admit scientific worker: image identity or upstream label differs from the selected inputs. '
        exit 2
    fi
    mkdir -m 700 -- "$scratch/controller/state"
fi
start_endpoint worker
if [[ $scenario == unstarted ]]; then
    retire_endpoint worker "$worker_id"
    finished=true
    exit 0
fi
await_bytes "$scratch/ipc/ready" "$allocation" 15
metadata running "$scratch/worker" inspect --type container --format '{{.Id}} {{.State.Running}}' "$worker_id"
if ! cmp -s -- "$scratch/worker/running.stdout" <(printf '%s true\n' "$worker_id"); then
    pending_diagnostic+='Cannot qualify retirement: the exact blocked worker was not observed running. '
    exit 2
fi
if [[ $scenario == geometry-* ]]; then
    metadata posture "$scratch/worker" inspect --type container --format \
        '{{.Id}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.NanoCpus}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{.Config.User}}|{{.HostConfig.RestartPolicy.Name}}' "$worker_id"
    if ! cmp -s -- "$scratch/worker/posture.stdout" <(printf '%s|true|none|false|134217728|134217728|32|1000000000|["ALL"]|["no-new-privileges:true"]|%s:%s|no\n' "$worker_id" "$caller_uid" "$caller_gid"); then
        pending_diagnostic+='Cannot qualify structure worker: observed engine isolation/resources differ from its fixed profile. '
        exit 2
    fi
fi
start_endpoint controller
retirement_request_seconds=15
if [[ $scenario == scientific ]]; then retirement_request_seconds=180; fi
await_bytes "$scratch/request/retire" "retire $allocation" "$retirement_request_seconds"
retire_endpoint worker "$worker_id"
rm -- "$scratch/ipc/ready"
if [[ $scenario == scientific ]]; then
    rm -- "$scratch/ipc/science.sock" "$scratch/ipc/metadata.json"
fi
if [[ $scenario == geometry-* ]]; then
    rm -- "$scratch/ipc/geometry.sock"
    if [[ $scenario == geometry-timeout ]]; then rm -- "$scratch/ipc/entered"; fi
fi
disposition=retired
if [[ $scenario == grant-failure ]]; then
    # Real rmdir failure: a retained grant entry prevents successful cleanup.
    # This fixture-only fault is removed after both containers have retired.
    printf '%s\n' retained > "$scratch/ipc/retained"
fi
if rmdir -- "$scratch/ipc" 2> "$scratch/grant-cleanup.stderr"; then
    if [[ $scenario != success && $scenario != scientific && $scenario != geometry-* ]]; then pending_diagnostic+='Expected grant-cleanup fault did not occur. '; exit 2; fi
elif [[ $scenario == grant-failure ]]; then disposition=unconfirmed
else pending_diagnostic+='Cannot confirm worker grant cleanup; inspect private cleanup evidence. '; exit 2
fi
printf '%s %s\n' "$disposition" "$allocation" > "$scratch/receipt/pending"
mv -- "$scratch/receipt/pending" "$scratch/receipt/result"
await_bytes "$scratch/request/observed" "$disposition $allocation" 45
metadata wait "$scratch/controller" wait "$controller_id"
if ! cmp -s -- "$scratch/controller/wait.stdout" <(printf '0\n'); then
    pending_diagnostic+='Contained controller did not accept the expected host disposition. '
    exit 2
fi
retire_endpoint controller "$controller_id"
if [[ $scenario == geometry-* ]]; then
    (cd -- "$repository_root" && sha256sum -- "${source_files[@]}") > "$scratch/sources-after.sha256"
    if ! cmp -s -- "$scratch/sources.sha256" "$scratch/sources-after.sha256"; then
        pending_diagnostic+='Cannot retain structure-worker evidence: source inputs changed during qualification. '
        exit 2
    fi
    qualification_receipt=$repository_root/.work/qualification/provider-geometry-$allocation
    mkdir -p -m 700 -- "$repository_root/.work/qualification"
    mkdir -m 700 -- "$qualification_receipt"
    cp -- "$scratch/request/evidence.json" "$qualification_receipt/summary.json"
    cp -- "$scratch/worker/retirement/inspect.stdout" "$qualification_receipt/worker-stopped.txt"
    cp -- "$scratch/worker/retirement/remove.stdout" "$qualification_receipt/worker-removed.txt"
    cp -- "$scratch/controller/retirement/inspect.stdout" "$qualification_receipt/controller-stopped.txt"
    cp -- "$scratch/worker/posture.stdout" "$qualification_receipt/worker-posture.txt"
    cp -- "$scratch/sources.sha256" "$qualification_receipt/sources.sha256"
    printf '%s\n' "$image_id" > "$qualification_receipt/image-id"
fi
if [[ $scenario == scientific ]]; then
    # Keep selected reproduction evidence, not disposable keys or successful
    # journals. Failed runs retain their complete original scratch instead.
    qualification_receipt=$repository_root/.work/qualification/provider-lifecycle-$allocation
    mkdir -p -m 700 -- "$repository_root/.work/qualification"
    mkdir -m 700 -- "$qualification_receipt"
    if [[ ! -f $scratch/controller/state/qualification.json || -L $scratch/controller/state/qualification.json ]]; then
        pending_diagnostic+='Scientific qualification did not produce its regular evidence summary. '
        exit 2
    fi
    cp -- "$scratch/controller/state/qualification.json" "$qualification_receipt/summary.json"
fi
finished=true
