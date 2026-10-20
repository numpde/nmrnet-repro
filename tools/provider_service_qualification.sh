#!/usr/bin/env bash
# One contained native service input witness, with production worker allocation.
# Local scripted TLS peers exercise the real service/factory/core; this is not a
# deployed API/model or a service installer. Profiles/retirement stay production-
# owned in provider_workers.sh; this caller owns only qualification and evidence.
set -euo pipefail
if [[ $# != 1 ]]; then
    printf '%s\n' 'Select exactly one native service fixture: uploads, pasted or smiles.' >&2
    exit 2
fi
provider_worker_seed=17 provider_worker_max_iterations=200
case "$1" in
    uploads)
        input_kind=upload
        fixture_files=(tests/fixtures/preparation/expected.json tests/fixtures/preparation/normative/mixed_records.xyz)
        ;;
    pasted)
        input_kind=pasted
        fixture_files=(tests/fixtures/preparation/expected.json)
        ;;
    smiles)
        input_kind=smiles
        fixture_files=(tests/fixtures/preparation/smiles.json)
        # Match the already-qualified CCCC/N=3 fixture, not the supplied-route settings.
        provider_worker_seed=20261019 provider_worker_max_iterations=1280
        ;;
    *) printf '%s\n' 'Select exactly one native service fixture: uploads, pasted or smiles.' >&2; exit 2 ;;
esac
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ $(id -u) == 0 || $(id -g) == 0 || $repository_root == *','* || $repository_root == *$'\n'* ]]; then
    printf '%s\n' 'Use make test/integration/provider-service/{uploads,pasted,smiles} from a nonroot checkout without commas or line breaks.' >&2
    exit 2
fi
umask 077
source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
source "$repository_root/tools/worker_retirement.sh"
source "$repository_root/tools/provider_workers.sh"
source "$repository_root/tools/git_metadata.sh"
docker_client_directory= pending_diagnostic= worker_retirement_diagnostic=
inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic=
scratch= controller_id= controller_attempted=false controller_removed=false finished=false root_open=false

command_receipt() {
    local operation=$1 directory=$2
    shift 2
    if worker_retirement_command "$operation" "$directory" "${engine[@]}" "$@"; then return 0; fi
    pending_diagnostic+=$worker_retirement_diagnostic
    return 2
}

finish() {
    local status=$? token cleanup=0
    trap - EXIT
    if [[ $inference_retirement_confirmed == true && $root_open == true ]]; then
        for token in "${!provider_worker_roles[@]}"; do
            if [[ ${provider_worker_retired[$token]} != true ]]; then
                provider_workers_retire "$token" || cleanup=$?
            fi
        done
        pending_diagnostic+=$provider_workers_diagnostic
    fi
    if [[ $inference_retirement_confirmed == true && -n $controller_id && $controller_attempted == false ]]; then
        controller_attempted=true
        if worker_container_retire "$controller_id" "$scratch/controller" "${engine[@]}"; then controller_removed=true
        else cleanup=$?; pending_diagnostic+=$worker_retirement_diagnostic; fi
    fi
    if [[ $root_open == true && $controller_removed == true && $inference_retirement_confirmed == true ]]; then
        provider_workers_close || cleanup=$?
    fi
    if [[ $status == 0 && $cleanup != 0 ]]; then status=$cleanup; fi
    if [[ $inference_retirement_confirmed == true ]]; then docker_client_cleanup || status=$?; fi
    if [[ $status == 0 && $finished == true && $controller_removed == true ]]; then
        rm -rf -- "$scratch" || status=$?
    elif [[ -n $scratch ]]; then
        printf -v pending_diagnostic '%s Native service qualification incomplete; preserve private ownership/evidence at %q.' "$pending_diagnostic" "$scratch"
        if [[ $status == 0 ]]; then status=2; fi
    fi
    if [[ -n $pending_diagnostic ]]; then printf '%s\n' "$pending_diagnostic" >&2; fi
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
scratch=$(mktemp -d /tmp/nmrnet-native-service-XXXXXXXX)
mkdir -- "$scratch/controller" "$scratch/controller/diagnostics" "$scratch/state"
docker_client_prepare run || { pending_diagnostic+=$docker_client_diagnostic; exit 2; }
repository_revision=$(repository_git -C "$repository_root" rev-parse --verify HEAD)
read -r mode revision stage path < <(repository_git -C "$repository_root" ls-files --stage -- NMRNetplusplus)
if [[ ! $repository_revision =~ ^[0-9a-f]{40}$ || $mode != 160000 || ! $revision =~ ^[0-9a-f]{40}$ || $stage != 0 ]]; then
    pending_diagnostic='Cannot qualify native service: full repository and unconflicted upstream revisions are required.'; exit 2
fi
for role in inference checker; do
    IFS= read -r candidate < "$repository_root/.work/$role/image-id"
    if [[ ! $candidate =~ ^sha256:[0-9a-f]{64}$ ]] || ! cmp -s -- "$repository_root/.work/$role/image-id" <(printf '%s\n' "$candidate"); then
        pending_diagnostic='Cannot qualify native service: image records must contain one byte-exact immutable ID.'; exit 2
    fi
    command_receipt "$role-image" "$scratch" image inspect --format '{{.Id}}' "$candidate"
    if ! cmp -s -- "$scratch/$role-image.stdout" <(printf '%s\n' "$candidate"); then
        pending_diagnostic='Cannot qualify native service: recorded and inspected image identities differ.'; exit 2
    fi
    if [[ $role == inference ]]; then scientific_image_id=$candidate; else controller_image_id=$candidate; fi
done
command_receipt upstream-image "$scratch" image inspect --format '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}' "$scientific_image_id"
if ! cmp -s -- "$scratch/upstream-image.stdout" <(printf '%s\n' "$revision"); then
    pending_diagnostic='Cannot qualify native service: prediction image upstream label differs from the selected revision.'; exit 2
fi
source "$repository_root/tools/geometry_image.sh"
print_command=false
require_geometry_image
provider_worker_accept_seconds=90 provider_worker_io_seconds=90 provider_source_io_seconds=5
provider_worker_embedding_seconds=1
provider_workers_open "$scratch/native" 2
root_open=true
source_files=(Makefile tools/provider_service_qualification.sh tools/provider_workers.sh tools/worker_qualification.sh tools/geometry_image.sh
    tools/worker_retirement.sh tools/inference_native.sh tools/docker_client.sh tools/git_metadata.sh
    requirements/provider-core.lock requirements/provider-core-build.lock requirements/inference.lock requirements/unicore-source.json
    requirements/preparation.lock tests/integration/provider_core/adversarial/__init__.py
    artifacts/prediction-models.json packages/provider-core/pyproject.toml
    packages/provider-core/tests/run_suite.py packages/provider-core/tests/__init__.py packages/provider-core/tests/integration/__init__.py
    "${fixture_files[@]}"
    .work/checker/image-id .work/inference/image-id .work/preparation/image-id)
for subtree in nmrnet_provider nmrnet_inference tools packages/provider-core/src/provider_core packages/provider-core/tests/fakes; do
    for source in "$repository_root/$subtree/"*.py; do source_files+=("${source#"$repository_root"/}"); done
done
for source in "$repository_root/tests/integration/provider_core/"*.py \
    "$repository_root/tests/integration/provider_core/normative/"*.py; do source_files+=("${source#"$repository_root"/}"); done
for source in "${source_files[@]}"; do
    if [[ ! -f $repository_root/$source || -L $repository_root/$source ]]; then
        pending_diagnostic='Cannot qualify native service: source closure contains a missing, nonregular or symlink input.'; exit 2
    fi
done
(cd -- "$repository_root" && sha256sum -- "${source_files[@]}") > "$scratch/sources.sha256"
grants=(--mount "type=bind,src=$scratch/native/requests,dst=/native/requests"
    --mount "type=bind,src=$scratch/native/receipts,dst=/native/receipts,readonly"
    --mount "type=bind,src=$scratch/native/ipc,dst=/native/ipc,readonly"
    --mount "type=bind,src=$scratch/state,dst=/state"
    --mount "type=bind,src=$scratch/controller/diagnostics,dst=/diagnostics"
    --mount "type=bind,src=$scratch/sources.sha256,dst=/input/sources.sha256,readonly"
    --mount "type=bind,src=$repository_root/artifacts/prediction-models.json,dst=/input/prediction-models.json,readonly"
    --mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
    --mount "type=bind,src=$repository_root/tools,dst=/consumer/tools,readonly"
    --mount "type=bind,src=$repository_root/nmrnet_provider,dst=/consumer/nmrnet_provider,readonly"
    --mount "type=bind,src=$repository_root/nmrnet_inference,dst=/consumer/nmrnet_inference,readonly"
    --mount "type=bind,src=$repository_root/tests/integration/provider_core,dst=/consumer-tests,readonly"
    --mount "type=bind,src=$repository_root/packages/provider-core,dst=/package,readonly"
    --mount "type=bind,src=$repository_root/requirements/provider-core-build.lock,dst=/core-build.lock,readonly"
    --mount "type=bind,src=$repository_root/requirements/provider-core.lock,dst=/core-runtime.lock,readonly"
    --mount "type=bind,src=$repository_root/.work/provider-core/wheelhouse,dst=/runtime-deps,readonly"
    --mount "type=bind,src=$repository_root/.work/wheelhouse/cpu/setuptools-84.0.0-py3-none-any.whl,dst=/build-deps/setuptools-84.0.0-py3-none-any.whl,readonly")
for fixture in "${fixture_files[@]}"; do
    grants+=(--mount "type=bind,src=$repository_root/$fixture,dst=/input/${fixture##*/},readonly")
done
if command_receipt create "$scratch/controller" create --cidfile "$scratch/controller/worker.cid" \
    --init --network none --pull never --read-only --restart no --log-driver none \
    --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true \
    --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --tmpfs /runtime:rw,nosuid,nodev,exec,size=128m --workdir /workspace \
    --env NMRNET_IN_CONTAINER=1 --env PROVIDER_CORE_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 \
    --env "NMRNET_LIVE_NATIVE_SERVICE=$input_kind" --env "NMRNET_QUALIFICATION_IMAGE_ID=$scientific_image_id" \
    --env "NMRNET_QUALIFICATION_CONTROLLER_IMAGE_ID=$controller_image_id" \
    --env "NMRNET_QUALIFICATION_GEOMETRY_IMAGE_ID=$geometry_image_id" \
    --env "NMRNET_QUALIFICATION_REPOSITORY_REVISION=$repository_revision" \
    "${grants[@]}" "$controller_image_id" bash -eu -c \
    'umask 077; ulimit -f 65536; exec python3 -B -m tools.provider_core_tests integration --pattern test_nmrnet_native_service.py > /diagnostics/stdout 2> /diagnostics/stderr'; then create_status=0; else create_status=$?; fi
if [[ $inference_retirement_confirmed == true && -f $scratch/controller/worker.cid && ! -L $scratch/controller/worker.cid ]] \
    && IFS= read -r -N 64 candidate < "$scratch/controller/worker.cid" && [[ $candidate =~ ^[0-9a-f]{64}$ ]] \
    && { cmp -s -- "$scratch/controller/worker.cid" <(printf '%s' "$candidate") \
        || cmp -s -- "$scratch/controller/worker.cid" <(printf '%s\n' "$candidate"); } \
    && cmp -s -- "$scratch/controller/create.stdout" <(printf '%s\n' "$candidate"); then controller_id=$candidate
else pending_diagnostic='Cannot admit native service controller: exact CID receipts differ; preserve the original create.'; exit 2; fi
if [[ $create_status != 0 ]]; then exit "$create_status"; fi
command_receipt start "$scratch/controller" start "$controller_id"
command_receipt posture "$scratch/controller" inspect --type container --format \
    '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.Running}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.NanoCpus}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{.Config.User}}|{{.HostConfig.RestartPolicy.Name}}' "$controller_id"
if ! cmp -s -- "$scratch/controller/posture.stdout" <(printf '%s|%s|running|true|true|none|false|536870912|536870912|64|2000000000|["ALL"]|["no-new-privileges:true"]|%s:%s|no\n' \
    "$controller_id" "$controller_image_id" "$(id -u)" "$(id -g)"); then
    pending_diagnostic='Cannot qualify native service controller: observed identity, running state or fixed isolation/resources differ.'; exit 2
fi
deadline=$((SECONDS + 180))
last_probe=$SECONDS
while (( SECONDS < deadline )); do
    # Independent host observation: no native create may precede both signed
    # startup admission and selected feed input in the contained service witness.
    for request in "$scratch/native/requests/"*/allocate; do
        [[ -f $request && ! -L $request ]] || continue
        if ! cmp -s -- "$scratch/state/admitted" <(printf 'admitted\n') || [[ ! -s $scratch/state/selected ]]; then
            pending_diagnostic='Native allocation preceded the witnessed service admission/selection.'; exit 2
        fi
    done
    if provider_workers_service; then :; else pending_diagnostic+=$provider_workers_diagnostic; exit 2; fi
    if [[ -f $scratch/state/finished ]] && cmp -s -- "$scratch/state/finished" <(printf 'qualified\n'); then break; fi
    if (( SECONDS >= last_probe + 2 )); then
        last_probe=$SECONDS
        probe_operation=state-$SECONDS
        command_receipt "$probe_operation" "$scratch/controller" inspect --type container --format '{{.State.Status}}' "$controller_id"
        if ! cmp -s -- "$scratch/controller/$probe_operation.stdout" <(printf 'running\n'); then
            pending_diagnostic='Native service controller stopped before qualification completed; preserve its protected diagnostics.'; exit 2
        fi
    fi
    sleep 0.05
done
if [[ ! -f $scratch/state/finished ]]; then pending_diagnostic='Native service witness exceeded its finite budget; preserve protected controller/worker diagnostics.'; exit 2; fi
command_receipt wait "$scratch/controller" wait "$controller_id"
if ! cmp -s -- "$scratch/controller/wait.stdout" <(printf '0\n'); then pending_diagnostic='Contained native service witness did not finish successfully.'; exit 2; fi
controller_attempted=true
worker_container_retire "$controller_id" "$scratch/controller" "${engine[@]}"
controller_removed=true
provider_workers_close
root_open=false
(cd -- "$repository_root" && sha256sum -- "${source_files[@]}") > "$scratch/sources-after.sha256"
if ! cmp -s -- "$scratch/sources.sha256" "$scratch/sources-after.sha256"; then pending_diagnostic='Native service source closure changed during qualification.'; exit 2; fi
receipt=$repository_root/.work/qualification/provider-service-${scratch##*/}
mkdir -p -m 700 -- "$repository_root/.work/qualification"
mkdir -m 700 -- "$receipt"
cp -- "$scratch/state/qualification.json" "$receipt/summary.json"
cp -- "$scratch/state/scientific-result.json" "$receipt/scientific-result.json"
cp -- "$scratch/sources.sha256" "$receipt/sources.sha256"
mkdir -- "$receipt/native-receipts"
for token in "${!provider_worker_roles[@]}"; do
    mkdir -- "$receipt/native-receipts/$token"
    cp -- "$scratch/native/owned/$token/create-intent" "$scratch/native/owned/$token/worker.cid" \
        "$scratch/native/owned/$token/create.stdout" "$scratch/native/owned/$token/posture.stdout" "$receipt/native-receipts/$token/"
    cp -r -- "$scratch/native/owned/$token/retirement" "$receipt/native-receipts/$token/retirement"
done
cp -r -- "$scratch/native/receipts" "$receipt/host-receipts"
cp -r -- "$scratch/controller/retirement" "$receipt/controller-retirement"
cp -- "$scratch/controller/posture.stdout" "$receipt/controller-posture.txt"
finished=true
printf 'Native local service qualification passed. Private receipt: %q\n' "$receipt"
