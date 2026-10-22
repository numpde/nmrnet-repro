#!/usr/bin/env bash
# One contained native service input witness, with production worker allocation.
# Local scripted TLS peers exercise the real service/factory/core; this is not a
# deployed API/model or a service installer. Profiles/retirement stay production-
# owned in provider_workers.sh; this caller owns only qualification and evidence.
provider_service_select_formats() {
    local selection=$1 name
    local -A seen=()
    provider_service_format_cases=(json table xyz mol sdf-all sdf-correction pdb-models pdb-alternate mol2)
    if [[ -z $selection ]]; then return 0; fi
    if [[ $selection == *$'\n'* || $selection == *$'\r'* ]]; then
        printf '%s\n' 'FORMAT_CASES must contain distinct fixed case names on one line.' >&2; return 2
    fi
    read -r -a provider_service_format_cases <<< "$selection"
    if (( ${#provider_service_format_cases[@]} == 0 )); then
        printf '%s\n' 'FORMAT_CASES must select at least one authored case.' >&2; return 2
    fi
    for name in "${provider_service_format_cases[@]}"; do
        case "$name" in json|table|xyz|mol|sdf-all|sdf-correction|pdb-models|pdb-alternate|mol2) ;; *)
            printf '%s\n' 'FORMAT_CASES accepts only the nine authored format case names.' >&2; return 2 ;;
        esac
        if [[ -v seen[$name] ]]; then
            printf '%s\n' 'FORMAT_CASES must not repeat a case.' >&2; return 2
        fi
        seen[$name]=true
    done
}

provider_service_format_request() {
    local state=$1 token=$2 name directory file size
    provider_service_format_diagnostic='Cannot admit format allocation: bounded case receipts could not be read.'
    if [[ ! $token =~ ^[0-9a-f]{32}$ || ! -f $state/active-case || -L $state/active-case ]] \
        || [[ $(stat -c '%s' -- "$state/active-case") -gt 32 ]]; then
        provider_service_format_diagnostic='Cannot admit format allocation: its case selector or token is outside the fixed qualification grammar.'
        return 2
    fi
    IFS= read -r name < "$state/active-case" || return 2
    case "$name" in json|table|xyz|mol|sdf-all|sdf-correction|pdb-models|pdb-alternate|mol2) ;; *)
        provider_service_format_diagnostic='Cannot admit format allocation: the active case is outside the fixed nine-case profile.'; return 2 ;;
    esac
    if ! cmp -s -- "$state/active-case" <(printf '%s\n' "$name"); then
        provider_service_format_diagnostic='Cannot admit format allocation: the active case must contain exactly one fixed name.'; return 2
    fi
    directory=$state/formats/$name
    if [[ ! -d $directory || -L $directory || ! -d $directory/allocation-intents || -L $directory/allocation-intents ]]; then
        provider_service_format_diagnostic='Cannot admit format allocation: its case receipt directory is missing or linked.'; return 2
    fi
    for file in admitted selected selection.json allocation-intents/$token; do
        if [[ ! -f $directory/$file || -L $directory/$file ]]; then
            provider_service_format_diagnostic='Cannot admit format allocation: a case admission or exact allocation receipt is missing or linked.'; return 2
        fi
        size=$(stat -c '%s' -- "$directory/$file") || return 2
        if (( size < 1 || size > 1096 )); then
            provider_service_format_diagnostic='Cannot admit format allocation: a case receipt exceeds its fixed byte bound.'; return 2
        fi
    done
    if ! cmp -s -- "$directory/admitted" <(printf 'admitted\n') \
        || [[ $(stat -c '%s' -- "$directory/selected") != 72 ]] \
        || ! IFS= read -r name < "$directory/selected" \
        || [[ ! $name =~ ^sha256:[0-9a-f]{64}$ ]] \
        || ! cmp -s -- "$directory/selected" <(printf '%s\n' "$name") \
        || ! cmp -s -- "$directory/allocation-intents/$token" <(cat -- "$directory/selected" "$directory/selection.json"); then
        provider_service_format_diagnostic='Cannot admit format allocation: exact case admission, selected Job or allocation intent differs.'; return 2
    fi
    provider_service_format_diagnostic=
    return 0
}

provider_service_qualification_main() {
set -euo pipefail
if [[ $# != 1 ]]; then
    printf '%s\n' 'Select one native service fixture: uploads, pasted, smiles, references, capacity or formats.' >&2
    exit 2
fi
provider_worker_seed=17 provider_worker_max_iterations=200
qualification_seconds=180 qualification_requests=4
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
    references)
        input_kind=references
        fixture_files=(artifacts/reference-cpu-float32.json artifacts/prediction-cpu-float32.json)
        ;;
    capacity)
        input_kind=capacity
        fixture_files=()
        ;;
    formats)
        input_kind=formats
        fixture_files=(tests/fixtures/preparation/expected.json tests/fixtures/preparation/README.txt
            tests/fixtures/preparation/normative/atom_order.json tests/fixtures/preparation/normative/mixed_records.xyz
            tests/fixtures/preparation/normative/atom_order_v2000.mol tests/fixtures/preparation/normative/mixed_records.sdf
            tests/fixtures/preparation/adversarial/invalid_last.sdf tests/fixtures/preparation/normative/models.pdb
            tests/fixtures/preparation/normative/alternate_locations.pdb tests/fixtures/preparation/normative/mixed_records.mol2)
        # Nine existing 180-second case budgets plus 120 seconds for startup.
        # Held cleanup retains ownership; this is not a hard cleanup deadline.
        qualification_seconds=1800 qualification_requests=22
        ;;
    *) printf '%s\n' 'Select one native service fixture: uploads, pasted, smiles, references, capacity or formats.' >&2; exit 2 ;;
esac
provider_service_format_cases=()
if [[ $input_kind == formats ]]; then provider_service_select_formats "${NMRNET_FORMAT_CASES_INPUT-}"
elif [[ -n ${NMRNET_FORMAT_CASES_INPUT-} ]]; then
    printf '%s\n' 'FORMAT_CASES is supported only by the native formats qualification.' >&2; exit 2
fi
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
if [[ $(id -u) == 0 || $(id -g) == 0 || $repository_root == *','* || $repository_root == *$'\n'* ]]; then
    printf '%s\n' 'Use make test/integration/provider-service/{uploads,pasted,smiles,references,capacity,formats} from a nonroot checkout without commas or line breaks.' >&2
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
producer_id= producer_attempted=false producer_removed=true

command_receipt() {
    local operation=$1 directory=$2
    shift 2
    if worker_retirement_command "$operation" "$directory" "${engine[@]}" "$@"; then return 0; fi
    pending_diagnostic+=$worker_retirement_diagnostic
    return 2
}

admit_created_container() {
    admitted_container_id=
    if worker_container_created "$1"; then
        admitted_container_id=$worker_created_id
        return 0
    fi
    pending_diagnostic+=$worker_retirement_diagnostic
    return 2
}

finish() {
    local status=$? token cleanup=0
    trap - EXIT
    if [[ $inference_retirement_confirmed == true && -n $producer_id && $producer_attempted == false ]]; then
        producer_attempted=true
        if worker_container_retire "$producer_id" "$scratch/producer" "${engine[@]}"; then producer_removed=true
        else cleanup=$?; pending_diagnostic+=$worker_retirement_diagnostic; fi
    fi
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
    if [[ $status == 0 && $finished == true && $controller_removed == true && $producer_removed == true ]]; then
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
provider_worker_accept_seconds=90 provider_worker_io_seconds=90 provider_source_io_seconds=10 provider_generation_io_seconds=5
provider_workers_capture_sources "$scratch/native-inputs" "$repository_revision"
provider_worker_prepared_root=$repository_root/.work/prepared
provider_worker_embedding_seconds=1
provider_workers_open "$scratch/native" "$qualification_requests" 2
root_open=true
source_files=(Makefile tools/provider.sh tools/provider_image.sh tools/provider_service_qualification.sh tools/provider_workers.sh tools/worker_qualification.sh tools/geometry_image.sh
    tools/worker_retirement.sh tools/inference_native.sh tools/docker_client.sh tools/git_inputs.sh tools/git_metadata.sh
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
if [[ $input_kind == references || $input_kind == capacity ]]; then
    source_files+=(tests/integration/provider_worker/reference_fixture.py tests/test_prediction_release.py tests/test_prediction_model.py)
fi
if [[ $input_kind == references ]]; then
    source_files+=(.work/baseline/prepared/provenance.json .work/baseline/prepared/H/valid.lmdb .work/baseline/prepared/H/valid_dbid.pkl
        .work/baseline/prepared/C/valid.lmdb .work/baseline/prepared/C/valid_dbid.pkl)
fi
for source in "${source_files[@]}"; do
    if [[ ! -f $repository_root/$source || -L $repository_root/$source ]]; then
        pending_diagnostic='Cannot qualify native service: source closure contains a missing, nonregular or symlink input.'; exit 2
    fi
done
(cd -- "$repository_root" && sha256sum -- "${source_files[@]}") > "$scratch/sources.sha256"
if ! provider_workers_match_source_manifest "$scratch/native-inputs/sources.sha256" "$scratch/sources.sha256"; then
    pending_diagnostic+=$provider_workers_diagnostic
    exit 2
fi
if [[ $input_kind == references || $input_kind == capacity ]]; then
    mkdir -- "$scratch/producer" "$scratch/producer/diagnostics" "$scratch/scientific-input"
    producer_removed=false
    producer_grants=(--mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
        --mount "type=bind,src=$repository_root/nmrnet_provider,dst=/workspace/nmrnet_provider,readonly"
        --mount "type=bind,src=$repository_root/nmrnet_inference,dst=/workspace/nmrnet_inference,readonly"
        --mount "type=bind,src=$repository_root/tests/test_prediction_release.py,dst=/workspace/tests/test_prediction_release.py,readonly"
        --mount "type=bind,src=$repository_root/tests/test_prediction_model.py,dst=/workspace/tests/test_prediction_model.py,readonly"
        --mount "type=bind,src=$repository_root/tests/integration/provider_core/reference_fixtures.py,dst=/workspace/tests/integration/provider_core/reference_fixtures.py,readonly"
        --mount "type=bind,src=$repository_root/tests/integration/provider_worker/reference_fixture.py,dst=/workspace/tests/integration/provider_worker/reference_fixture.py,readonly"
        --mount "type=bind,src=$repository_root/requirements/inference.lock,dst=/input/inference.lock,readonly"
        --mount "type=bind,src=$repository_root/requirements/unicore-source.json,dst=/input/unicore-source.json,readonly"
        --mount "type=bind,src=$scratch/scientific-input,dst=/output"
        --mount "type=bind,src=$scratch/producer/diagnostics,dst=/diagnostics")
    if [[ $input_kind == references ]]; then
        producer_grants+=(--mount "type=bind,src=$repository_root/artifacts/reference-cpu-float32.json,dst=/input/reference-cpu-float32.json,readonly")
        for source in provenance.json H/valid.lmdb H/valid_dbid.pkl C/valid.lmdb C/valid_dbid.pkl; do
            producer_grants+=(--mount "type=bind,src=$repository_root/.work/baseline/prepared/$source,dst=/prepared/$source,readonly")
        done
    fi
    # The decoder gets no model weights, API/store credentials or native IPC.
    # Only a completed, exactly retired producer may hand its private files on.
    if command_receipt create "$scratch/producer" create --cidfile "$scratch/producer/worker.cid" \
        --init --network none --pull never --read-only --restart no --log-driver none \
        --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus 2 --memory 1g --memory-swap 1g --pids-limit 64 \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --workdir /workspace \
        --env NMRNET_IN_CONTAINER=1 --env NMRNET_PREDICTION_PARITY=1 --env PYTHONDONTWRITEBYTECODE=1 \
        --env PYTHONPATH=/workspace:/workspace/tests:/workspace/tests/integration/provider_core:/opt/nmrnet \
        --env "NMRNET_QUALIFICATION_IMAGE_ID=$scientific_image_id" \
        --env "NMRNET_QUALIFICATION_REPOSITORY_REVISION=$repository_revision" \
        --env "NMRNET_QUALIFICATION_UPSTREAM_REVISION=$revision" \
        "${producer_grants[@]}" "$scientific_image_id" bash -eu -c \
        'umask 077; ulimit -f 2048; exec python3 -B /workspace/tests/integration/provider_worker/reference_fixture.py "$1" > /diagnostics/stdout 2> /diagnostics/stderr' \
        scientific-input "$input_kind"; then producer_create_status=0; else producer_create_status=$?; fi
    admit_created_container "$scratch/producer"
    producer_id=$admitted_container_id
    if [[ $producer_create_status != 0 ]]; then exit "$producer_create_status"; fi
    command_receipt posture "$scratch/producer" inspect --type container --format \
        '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.Running}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.NanoCpus}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{.Config.User}}|{{.HostConfig.RestartPolicy.Name}}' "$producer_id"
    if ! cmp -s -- "$scratch/producer/posture.stdout" <(printf '%s|%s|created|false|true|none|false|1073741824|1073741824|64|2000000000|["ALL"]|["no-new-privileges:true"]|%s:%s|no\n' \
        "$producer_id" "$scientific_image_id" "$(id -u)" "$(id -g)"); then
        pending_diagnostic='Cannot qualify scientific input producer: exact image/isolation/resource posture differs.'; exit 2
    fi
    command_receipt start "$scratch/producer" start "$producer_id"
    command_receipt wait "$scratch/producer" wait "$producer_id"
    if ! cmp -s -- "$scratch/producer/wait.stdout" <(printf '0\n'); then
        pending_diagnostic='Scientific input producer failed; preserve its protected diagnostics and exact ownership.'; exit 2
    fi
    producer_attempted=true
    if worker_container_retire "$producer_id" "$scratch/producer" "${engine[@]}"; then :; else
        pending_diagnostic+=$worker_retirement_diagnostic
        exit 2
    fi
    producer_removed=true
    for source in input.xyz manifest.json; do
        if [[ ! -f $scratch/scientific-input/$source || -L $scratch/scientific-input/$source ]]; then
            pending_diagnostic='Scientific input producer did not leave regular private input and manifest files.'; exit 2
        fi
    done
fi
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
    if [[ $input_kind == formats ]]; then
        grants+=(--mount "type=bind,src=$repository_root/$fixture,dst=/input/preparation/${fixture#tests/fixtures/preparation/},readonly")
    else
        grants+=(--mount "type=bind,src=$repository_root/$fixture,dst=/input/${fixture##*/},readonly")
    fi
done
if [[ $input_kind == references || $input_kind == capacity ]]; then
    grants+=(--mount "type=bind,src=$scratch/scientific-input,dst=/input/scientific,readonly")
fi
if command_receipt create "$scratch/controller" create --cidfile "$scratch/controller/worker.cid" \
    --init --network none --pull never --read-only --restart no --log-driver none \
    --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true \
    --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64 \
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --tmpfs /runtime:rw,nosuid,nodev,exec,size=128m --workdir /workspace \
    --env NMRNET_IN_CONTAINER=1 --env PROVIDER_CORE_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 \
    --env "NMRNET_LIVE_NATIVE_SERVICE=$input_kind" --env "NMRNET_QUALIFICATION_IMAGE_ID=$scientific_image_id" \
    --env "NMRNET_QUALIFICATION_FORMAT_CASES=${provider_service_format_cases[*]}" \
    --env "NMRNET_QUALIFICATION_CONTROLLER_IMAGE_ID=$controller_image_id" \
    --env "NMRNET_QUALIFICATION_GEOMETRY_IMAGE_ID=$geometry_image_id" \
    --env "NMRNET_QUALIFICATION_REPOSITORY_REVISION=$repository_revision" \
    --env "NMRNET_QUALIFICATION_UPSTREAM_REVISION=$revision" \
    "${grants[@]}" "$controller_image_id" bash -eu -c \
    'umask 077; ulimit -f 65536; exec python3 -B -m tools.provider_core_tests integration --pattern test_nmrnet_native_service.py > /diagnostics/stdout 2> /diagnostics/stderr'; then create_status=0; else create_status=$?; fi
admit_created_container "$scratch/controller"
controller_id=$admitted_container_id
if [[ $create_status != 0 ]]; then exit "$create_status"; fi
command_receipt start "$scratch/controller" start "$controller_id"
command_receipt posture "$scratch/controller" inspect --type container --format \
    '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.Running}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.NanoCpus}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{.Config.User}}|{{.HostConfig.RestartPolicy.Name}}' "$controller_id"
if ! cmp -s -- "$scratch/controller/posture.stdout" <(printf '%s|%s|running|true|true|none|false|536870912|536870912|64|2000000000|["ALL"]|["no-new-privileges:true"]|%s:%s|no\n' \
    "$controller_id" "$controller_image_id" "$(id -u)" "$(id -g)"); then
    pending_diagnostic='Cannot qualify native service controller: observed identity, running state or fixed isolation/resources differ.'; exit 2
fi
deadline=$((SECONDS + qualification_seconds))
last_probe=$SECONDS
while (( SECONDS < deadline )); do
    # Independent host observation: no native create may precede both signed
    # startup admission and selected feed input in the contained service witness.
    for request in "$scratch/native/requests/"*/allocate; do
        [[ -f $request && ! -L $request ]] || continue
        if [[ $input_kind == formats ]]; then
            token=${request%/allocate}; token=${token##*/}
            [[ -z ${provider_worker_roles[$token]-} ]] || continue
            if ! provider_service_format_request "$scratch/state" "$token"; then
                pending_diagnostic+=$provider_service_format_diagnostic; exit 2
            fi
        elif ! cmp -s -- "$scratch/state/admitted" <(printf 'admitted\n') || [[ ! -s $scratch/state/selected ]]; then
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
if worker_container_retire "$controller_id" "$scratch/controller" "${engine[@]}"; then :; else
    pending_diagnostic+=$worker_retirement_diagnostic
    exit 2
fi
controller_removed=true
provider_workers_close
root_open=false
(cd -- "$repository_root" && sha256sum -- "${source_files[@]}") > "$scratch/sources-after.sha256"
if ! cmp -s -- "$scratch/sources.sha256" "$scratch/sources-after.sha256"; then pending_diagnostic='Native service source closure changed during qualification.'; exit 2; fi
receipt=$repository_root/.work/qualification/provider-service-${scratch##*/}
mkdir -p -m 700 -- "$repository_root/.work/qualification"
mkdir -m 700 -- "$receipt"
cp -- "$scratch/state/qualification.json" "$receipt/summary.json"
if [[ $input_kind == formats ]]; then
    cp -r -- "$scratch/state/formats" "$receipt/formats"
else
    cp -- "$scratch/state/scientific-result.json" "$receipt/scientific-result.json"
fi
cp -- "$scratch/sources.sha256" "$receipt/sources.sha256"
mkdir -- "$receipt/native-inputs"
cp -- "$scratch/native-inputs/revision" "$scratch/native-inputs/inputs" "$scratch/native-inputs/inputs.paths" \
    "$scratch/native-inputs/sources.sha256" "$receipt/native-inputs/"
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
if [[ $input_kind == references || $input_kind == capacity ]]; then
    mkdir -- "$receipt/producer"
    cp -- "$scratch/scientific-input/input.xyz" "$scratch/scientific-input/manifest.json" "$receipt/producer/"
    cp -- "$scratch/producer/worker.cid" "$scratch/producer/create.stdout" "$scratch/producer/posture.stdout" "$scratch/producer/wait.stdout" "$receipt/producer/"
    cp -r -- "$scratch/producer/retirement" "$receipt/producer/retirement"
fi
finished=true
printf 'Native local service qualification passed. Private receipt: %q\n' "$receipt"
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then provider_service_qualification_main "$@"; fi
