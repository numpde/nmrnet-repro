#!/usr/bin/env bash
# Source-only host allocation owner. Caller supplies admitted immutable images,
# source/artifact roots and finite native budgets, owns traps/controller shutdown,
# and holds this root until every native obligation is confirmed. No Job fields
# can select images, commands, paths, resources or network settings.
# Create receipts adapt worker_qualification.sh at 02a4f77:355-385; exact retirement
# stays in worker_retirement.sh. Engine calls remain serial: inference_native.sh
# retains one original native-client ownership fact across interrupted observations.

source "$(dirname -- "${BASH_SOURCE[0]}")/provider_logging.sh"

provider_workers_capture_sources() {
    local destination=$1 expected_revision=$2 commit changes source
    provider_worker_source_root= provider_worker_source_revision=
    local -a files=(Makefile tools/provider.sh tools/provider_workers.sh tools/provider_logging.sh tools/docker_client.sh tools/inference_native.sh
        tools/worker_retirement.sh tools/provider_image.sh tools/geometry_image.sh
        tools/git_inputs.sh tools/git_metadata.sh tools/container_guard.py tools/container_resources.py
        tools/artifacts.py tools/baseline_prepare.py tools/provider_proxy.py tools/bound_http_proxy.py
        requirements/inference.lock requirements/unicore-source.json artifacts/prediction-models.json)
    # Upstream model code remains image-owned. Capture only the Python/declaration
    # files granted by these worker profiles and the later service relay.
    for source in "$repository_root"/nmrnet_provider/*.py "$repository_root"/nmrnet_inference/*.py; do
        files+=("${source#"$repository_root"/}")
    done
    for source in nmrnet_provider/worker_main.py nmrnet_provider/geometry_worker.py nmrnet_provider/smiles_worker.py \
        nmrnet_inference/predict.py nmrnet_inference/model.py nmrnet_inference/release.py nmrnet_inference/worker.py; do
        if [[ ! -f $repository_root/$source || -L $repository_root/$source ]]; then
            provider_workers_diagnostic='Cannot capture native sources: a required worker entrypoint or inference module is missing or linked.'
            return 2
        fi
    done
    source "$repository_root/tools/git_inputs.sh" || return $?
    commit=$(repository_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}') || return $?
    if [[ ! $commit =~ ^[0-9a-f]{40}$ ]]; then
        provider_workers_diagnostic='Cannot capture native sources: require a full committed source identity.'
        return 2
    fi
    if [[ $commit != "$expected_revision" ]]; then
        provider_workers_diagnostic='Cannot capture native sources: HEAD differs from the caller-declared execution revision.'
        return 2
    fi
    changes=$(repository_git -C "$repository_root" status --porcelain --untracked-files=all -- \
        "${files[@]}" nmrnet_provider nmrnet_inference) || return $?
    if [[ -n $changes ]]; then
        provider_workers_diagnostic='Cannot capture native sources: commit their bytes and executable modes before starting workers.'
        return 2
    fi
    mkdir -m 700 -- "$destination" "$destination/sources" || return $?
    (umask 077; copy_git_blobs 'native worker sources' "$repository_root" "$commit" "$destination/sources" \
        "$destination/inputs" "${files[@]}") || return $?
    if ! cmp -s -- "$destination/inputs.paths" <(printf '%s\n' "${files[@]}" | LC_ALL=C sort); then
        provider_workers_diagnostic='Cannot capture native sources: the committed input closure is incomplete.'
        return 2
    fi
    (umask 077; cd -- "$destination/sources" && sha256sum -- "${files[@]}" > "$destination/sources.sha256") || return $?
    (umask 077; printf '%s\n' "$commit" > "$destination/revision") || return $?
    provider_worker_source_root=$destination/sources provider_worker_source_revision=$commit
}

provider_workers_match_source_manifest() {
    local captured=$1 declared=$2 row
    while IFS= read -r row; do
        if ! grep -Fqx -- "$row" "$declared"; then
            provider_workers_diagnostic='Cannot qualify native sources: the declared source manifest differs from the frozen worker inputs.'
            return 2
        fi
    done < "$captured"
}

provider_workers_open() {
    local root=$1 maximum=$2 preparation_calls=$3
    provider_workers_diagnostic=
    if [[ ! $maximum =~ ^[1-9][0-9]{0,5}$ || -L $root || ( -e $root && ! -d $root ) ]]; then
        provider_workers_diagnostic='Cannot open native allocation root: require a private directory without a symlink and an explicit positive request bound.'
        return 2
    fi
    if [[ ! $preparation_calls =~ ^[1-9][0-9]{0,5}$ ]] || (( maximum < preparation_calls + 2 )); then
        provider_workers_diagnostic='Cannot open native allocation: the request bound must cover prediction, every allowed source call and one refused request awaiting retirement.'
        return 2
    fi
    if [[ ! -d $root ]]; then (umask 077; mkdir -- "$root") || return $?; fi
    if [[ $(stat -c '%u:%a' -- "$root") != "$(id -u):700" ]]; then
        provider_workers_diagnostic='Cannot open native allocation root: it must be caller-owned with mode 0700.'
        return 2
    fi
    exec {provider_workers_lock}< "$root" || return $?
    if ! flock --nonblock "$provider_workers_lock"; then
        provider_workers_diagnostic='Cannot start native allocation: another host owner holds this root.'
        exec {provider_workers_lock}<&-
        return 2
    fi
    # Prior leaves require stopped inspection/archive, including confirmed leaves;
    # never overwrite an earlier installation's uncertain create or private evidence.
    if ! (shopt -s dotglob nullglob; entries=("$root"/*); (( ${#entries[@]} == 0 ))); then
        provider_workers_diagnostic='Cannot reuse native allocation root: prior evidence requires stopped inspection and archival before a new installation.'
        exec {provider_workers_lock}<&-
        return 2
    fi
    provider_workers_root=$root provider_workers_maximum=$maximum
    provider_workers_preparation_calls=$preparation_calls
    provider_workers_admission=open
    provider_workers_creation_deadline=0
    # These children become bind roots; the private host ancestor is not part
    # of the worker/controller namespace. Do not depend on the caller's umask.
    mkdir -m 700 -- "$root/requests" "$root/receipts" "$root/ipc" "$root/owned" "$root/history" || return $?
    declare -gA provider_worker_ids=() provider_worker_roles=() provider_worker_retired=()
}

provider_workers_command() {
    local operation=$1 directory=$2
    shift 2
    if worker_retirement_command "$operation" "$directory" "${engine[@]}" "$@"; then return 0; fi
    provider_workers_diagnostic+=$worker_retirement_diagnostic
    return 2
}

provider_workers_publish() {
    local token=$1 name=$2 content=$3 receipt=$provider_workers_root/receipts/$1
    if [[ -e $receipt/$name || -L $receipt/$name ]]; then
        provider_workers_diagnostic='Cannot publish native allocation receipt: earlier evidence already occupies its destination.'
        return 2
    fi
    (umask 077; set -o noclobber; printf '%s\n' "$content" > "$receipt/pending") || return $?
    mv -- "$receipt/pending" "$receipt/$name"
}

provider_workers_create() {
    local role=$1 token=$2 directory=$provider_workers_root/owned/$2 image memory memory_bytes interpreter
    local identifier= create_status=0
    provider_logging_configure || return $?
    local io_seconds=$provider_worker_io_seconds
    if [[ $role == reader ]]; then io_seconds=$provider_source_io_seconds; fi
    if [[ $role == smiles ]]; then io_seconds=$provider_generation_io_seconds; fi
    local -a grants=(--mount "type=bind,src=$provider_workers_root/ipc/$token,dst=/ipc"
        --mount "type=bind,src=$directory/diagnostics,dst=/diagnostics"
        --mount "type=bind,src=$provider_worker_source_root/nmrnet_provider,dst=/workspace/nmrnet_provider,readonly"
        --mount "type=bind,src=$provider_worker_source_root/tools/container_resources.py,dst=/workspace/tools/container_resources.py,readonly"
        --mount "type=bind,src=$provider_worker_source_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly")
    local -a arguments=("$role" "$token" --accept-seconds "$provider_worker_accept_seconds" --io-seconds "$io_seconds")
    if [[ $role == prediction ]]; then
        image=$scientific_image_id memory=4g memory_bytes=4294967296 interpreter=python3
        grants+=(--mount "type=bind,src=$provider_worker_source_root/nmrnet_inference,dst=/workspace/nmrnet_inference,readonly"
            --mount "type=bind,src=$provider_worker_source_root/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
            --mount "type=bind,src=$provider_worker_source_root/tools/baseline_prepare.py,dst=/workspace/tools/baseline_prepare.py,readonly"
            --mount "type=bind,src=$provider_worker_source_root/artifacts/prediction-models.json,dst=/input/prediction-models.json,readonly"
            --mount "type=bind,src=$provider_worker_prepared_root/weight,dst=/weight,readonly"
            --mount "type=bind,src=$provider_worker_prepared_root/checkpoints,dst=/checkpoints,readonly"
            --mount "type=bind,src=$provider_worker_source_root/requirements/inference.lock,dst=/input/inference.lock,readonly"
            --mount "type=bind,src=$provider_worker_source_root/requirements/unicore-source.json,dst=/input/unicore-source.json,readonly")
    else
        image=$geometry_image_id memory=512m memory_bytes=536870912 interpreter=/opt/rdkit/environment/bin/python
        grants+=(--mount "type=bind,src=$provider_worker_source_root/nmrnet_inference/predict.py,dst=/workspace/nmrnet_inference/predict.py,readonly")
        if [[ $role == smiles ]]; then
            arguments+=(--seed "$provider_worker_seed" --max-iterations "$provider_worker_max_iterations"
                --embedding-seconds "$provider_worker_embedding_seconds")
        fi
    fi
    # Retain ownership before dispatch, including a failed/late create whose CID
    # cannot be admitted. An empty ID blocks replacement and root release.
    provider_worker_ids[$token]= provider_worker_roles[$token]=$role provider_worker_retired[$token]=false
    mkdir -m 700 -- "$directory" "$directory/diagnostics" "$provider_workers_root/ipc/$token" || return $?
    (umask 077; printf '%s %s\n' "$role" "$token" > "$directory/create-intent") || return $?
    if provider_workers_command create "$directory" create --cidfile "$directory/worker.cid" \
        --init --network none --pull never --read-only --restart no "${provider_logging_options[@]}" \
        --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus 2 --memory "$memory" --memory-swap "$memory" --pids-limit 64 \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --workdir /workspace \
        --env NMRNET_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 --env "NMRNET_LOG_LEVEL=$provider_log_level" \
        "${grants[@]}" "$image" bash -eu -c \
        'umask 077; ulimit -f 64; exec "$@"' \
        bash "$interpreter" -B -m nmrnet_provider.worker_main "${arguments[@]}"; then :; else create_status=$?; fi
    if worker_container_created "$directory"; then
        identifier=$worker_created_id
        provider_worker_ids[$token]=$identifier
    else
        provider_workers_diagnostic+=$worker_retirement_diagnostic
        return 2
    fi
    if [[ $create_status != 0 ]]; then return "$create_status"; fi
    if [[ $provider_workers_admission != open ]] \
        || (( provider_workers_creation_deadline != 0 && SECONDS >= provider_workers_creation_deadline )); then
        provider_workers_admission=closed
        provider_workers_retire "$token"
        return $?
    fi
    # Retirement can arrive while create is in flight. Do not start that worker.
    if provider_workers_retirement_requested "$token"; then provider_workers_retire "$token"; return $?; fi
    provider_workers_command start "$directory" start "$identifier" || return $?
    provider_workers_command posture "$directory" inspect --type container --format \
        '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.Running}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.NanoCpus}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{.Config.User}}|{{.HostConfig.RestartPolicy.Name}}|{{.HostConfig.LogConfig.Type}}|{{json .HostConfig.LogConfig.Config}}' "$identifier" || return $?
    if ! cmp -s -- "$directory/posture.stdout" <(printf '%s|%s|running|true|true|none|false|%s|%s|64|2000000000|["ALL"]|["no-new-privileges:true"]|%s:%s|no|%s\n' \
        "$identifier" "$image" "$memory_bytes" "$memory_bytes" "$(id -u)" "$(id -g)" "$provider_logging_posture"); then
        provider_workers_diagnostic+='Cannot admit native worker: observed identity, running state or fixed isolation/resources differ; retire this exact allocation. '
        return 2
    fi
    if provider_workers_retirement_requested "$token"; then provider_workers_retire "$token"; return $?; fi
    provider_workers_publish "$token" allocated "allocated $role $token"
}

provider_workers_retirement_requested() {
    local request=$provider_workers_root/requests/$1/retire
    [[ -f $request && ! -L $request ]] && cmp -s -- "$request" <(printf 'retire %s\n' "$1")
}

provider_workers_observe_final_report() {
    # A normal closed channel may finish its snapshot just after the controller
    # requests retirement. Give it at most one second. Emergency/late-create
    # retirement calls provider_workers_retire directly and never waits here.
    local report=$provider_workers_root/owned/$1/diagnostics/resources.json deadline=$((SECONDS + 1))
    while (( SECONDS < deadline )); do
        if [[ -f $report && ! -L $report ]]; then return 0; fi
        sleep 0.05
    done
}

provider_workers_retire() {
    local token=$1 directory=$provider_workers_root/owned/$1 identifier=${provider_worker_ids[$1]}
    if [[ ${provider_worker_retired[$token]} == true ]]; then return 0; fi
    if [[ -z $identifier ]]; then
        provider_workers_diagnostic+='Cannot retire an uncertain native create: retain its exact original engine ownership and receipts. '
        return 2
    fi
    if worker_container_retire "$identifier" "$directory" "${engine[@]}"; then :; else
        provider_workers_diagnostic+=$worker_retirement_diagnostic
        return 2
    fi
    # Remove only this role's known IPC entries. Unexpected files hold retirement,
    # rather than granting a recursive delete over worker-controlled names.
    local ipc=$provider_workers_root/ipc/$token
    if [[ ${provider_worker_roles[$token]} == prediction ]]; then
        rm -f -- "$ipc/science.sock" "$ipc/metadata.json" "$ipc/ready" || return $?
    else
        rm -f -- "$ipc/geometry.sock" "$ipc/ready" || return $?
    fi
    rmdir -- "$ipc" || return $?
    # The exact container is gone, so it cannot race these checks or change the
    # report. Only this small completed file crosses into controller-readable
    # receipts; missing evidence must never prevent retirement confirmation.
    local report=$directory/diagnostics/resources.json observation_status=0
    if [[ -f $report && ! -L $report && $(stat -c '%s' -- "$report") -le 16384 ]]; then
        if ! ln -- "$report" "$provider_workers_root/receipts/$token/resources.json"; then
            provider_workers_diagnostic+='Native worker is removed, but its final resource report could not be retained. '
            observation_status=2
        fi
    fi
    provider_workers_publish "$token" result "retired $token" || return $?
    provider_worker_retired[$token]=true
    return "$observation_status"
}

provider_workers_archive() {
    # Consumption follows the client's original setup/channel settlement. This
    # host still independently requires its own exact retirement observation.
    # Same-filesystem renames preserve original bytes; this shell protocol does
    # not claim directory-fsync or power-loss durability (note 017).
    local token=$1 request=$provider_workers_root/requests/$1
    local owned=$provider_workers_root/owned/$1 history=$provider_workers_root/history/$1
    local receipt=$provider_workers_root/receipts/$1 report=$provider_workers_root/owned/$1/diagnostics/resources.json
    local history_device request_device owned_device report_size
    if [[ ${provider_worker_retired[$token]-} != true || ! -f $request/consumed || -L $request/consumed ]] \
        || ! cmp -s -- "$request/consumed" <(printf 'consumed %s\n' "$token") \
        || [[ ! -f $receipt/result || -L $receipt/result ]] \
        || ! cmp -s -- "$receipt/result" <(printf 'retired %s\n' "$token"); then
        provider_workers_diagnostic='Cannot renew native capacity: consumption must match the exact independently retired allocation.'
        return 2
    fi
    if [[ -e $history || -L $history || -e $provider_workers_root/ipc/$token || -L $provider_workers_root/ipc/$token ]]; then
        provider_workers_diagnostic='Cannot renew native capacity: an earlier archive or unresolved IPC grant requires stopped reconciliation.'
        return 2
    fi
    if ! history_device=$(stat -c '%d' -- "$provider_workers_root/history") \
        || ! request_device=$(stat -c '%d' -- "$request"); then
        provider_workers_diagnostic='Cannot renew native capacity: filesystem identity observation failed; preserve every active path.'
        return 2
    fi
    if [[ -n ${provider_worker_roles[$token]} ]]; then
        if [[ ! -d $owned || -L $owned ]] || ! owned_device=$(stat -c '%d' -- "$owned") \
            || [[ $owned_device != "$history_device" ]]; then
            provider_workers_diagnostic='Cannot renew native capacity: preserve the original owned worker tree on the same filesystem.'
            return 2
        fi
    elif [[ -n ${provider_worker_ids[$token]} || -e $owned || -L $owned ]]; then
        provider_workers_diagnostic='Cannot renew native capacity: an absent worker tree does not establish a never-dispatched allocation.'
        return 2
    fi
    # A prior report-link failure must not become successful renewal merely
    # because physical retirement subsequently became observable by the client.
    if [[ -f $report && ! -L $report ]]; then
        if ! report_size=$(stat -c '%s' -- "$report"); then
            provider_workers_diagnostic='Cannot renew native capacity: final resource-report size could not be observed; preserve its evidence.'
            return 2
        fi
        if (( report_size <= 16384 )) \
            && { [[ ! -f $receipt/resources.json || -L $receipt/resources.json ]] || ! cmp -s -- "$report" "$receipt/resources.json"; }; then
            provider_workers_diagnostic='Cannot renew native capacity: the final resource report was not retained at its receipt boundary.'
            return 2
        fi
    fi
    if [[ $request_device != "$history_device" ]]; then
        provider_workers_diagnostic='Cannot renew native capacity: request preservation requires the same filesystem.'
        return 2
    fi
    if ! mkdir -m 700 -- "$history"; then
        provider_workers_diagnostic='Cannot renew native capacity: private history creation failed; preserve the active allocation and any partial history.'
        return 2
    fi
    if [[ -n ${provider_worker_roles[$token]} ]] && ! mv -T -- "$owned" "$history/owned"; then
        provider_workers_diagnostic='Cannot renew native capacity: worker-tree preservation is unconfirmed; retain every active/history path.'
        return 2
    fi
    if ! mv -T -- "$request" "$history/requests"; then
        provider_workers_diagnostic='Cannot renew native capacity: request-tree preservation is unconfirmed; retain every active/history path.'
        return 2
    fi
    # Receipts stay in their original read-only grant. A failed marker leaves
    # the existing maps held; the next service call refuses missing active paths.
    if ! provider_workers_publish "$token" archived "archived $token"; then
        provider_workers_diagnostic='Cannot renew native capacity: history was moved but its archive receipt is unconfirmed; preserve all paths and held ownership.'
        return 2
    fi
    unset 'provider_worker_ids[$token]' 'provider_worker_roles[$token]' 'provider_worker_retired[$token]'
}

provider_workers_service() {
    local request token role count=0 candidate other
    local -a tokens=()
    for token in "${!provider_worker_roles[@]}"; do
        if [[ ! -d $provider_workers_root/requests/$token || -L $provider_workers_root/requests/$token ]]; then
            provider_workers_diagnostic='Cannot service native requests: a held allocation lost its active request path during an unconfirmed transfer; preserve history and reconcile storage.'
            return 2
        fi
    done
    for request in "$provider_workers_root/requests/"*; do
        [[ -e $request || -L $request ]] || continue
        token=${request##*/}
        if [[ ! $token =~ ^[0-9a-f]{32}$ || ! -d $request || -L $request ]]; then
            provider_workers_diagnostic='Cannot service native requests: request count or private allocation directory is outside its grant.'
            return 2
        fi
        if [[ ! ${provider_worker_roles[$token]+known} ]] \
            && [[ -e $provider_workers_root/receipts/$token || -L $provider_workers_root/receipts/$token \
                || -e $provider_workers_root/history/$token || -L $provider_workers_root/history/$token ]]; then
            provider_workers_diagnostic='Cannot service native allocation: this token already has retained receipts or history; use no earlier token as fresh work.'
            return 2
        fi
        # Drain completed handoffs before counting live request capacity. A
        # physically retired but unconsumed request continues to count.
        if [[ -e $request/consumed || -L $request/consumed ]]; then
            provider_workers_archive "$token" || return $?
            continue
        fi
        count=$((count + 1))
        if (( count > provider_workers_maximum )); then
            provider_workers_diagnostic='Cannot service native requests: active request count exceeds its grant; preserve every allocation.'
            return 2
        fi
        tokens+=("$token")
        [[ ${provider_worker_retired[$token]-} != true ]] || continue
        if [[ -n ${provider_worker_roles[$token]-} ]] && provider_workers_retirement_requested "$token"; then
            provider_workers_observe_final_report "$token"
            provider_workers_retire "$token" || return $?
        elif [[ -z ${provider_worker_roles[$token]-} ]] && provider_workers_retirement_requested "$token"; then
            # This sole host has never dispatched this token. Retirement also
            # works after admission closes, with or without a complete allocate.
            provider_worker_ids[$token]= provider_worker_roles[$token]= provider_worker_retired[$token]=true
            mkdir -m 700 -- "$provider_workers_root/receipts/$token" || return $?
            provider_workers_publish "$token" result "retired $token" || return $?
        fi
    done
    [[ $provider_workers_admission == open ]] || return 0
    for token in "${tokens[@]}"; do
        # The foreground caller supplies one absolute drain deadline. Recheck
        # between creates: a preceding engine call may consume its remainder.
        if (( provider_workers_creation_deadline != 0 && SECONDS >= provider_workers_creation_deadline )); then
            provider_workers_admission=closed
            return 0
        fi
        [[ ${provider_worker_retired[$token]-} != true ]] || continue
        [[ -z ${provider_worker_roles[$token]-} ]] || continue
        request=$provider_workers_root/requests/$token/allocate
        [[ -f $request && ! -L $request ]] || continue
        role=
        for candidate in prediction reader smiles; do
            if cmp -s -- "$request" <(printf 'allocate %s %s\n' "$candidate" "$token"); then role=$candidate; break; fi
        done
        if [[ -z $role ]]; then
            provider_workers_diagnostic='Cannot service native allocation: complete request must contain only an admitted role and its exact token.'
            return 2
        fi
        # Count already includes this prediction. Keep room for every source
        # call and the next refused prediction's retirement, without exceeding
        # the physical request-leaf bound or deleting earlier evidence.
        if [[ $role == prediction ]] && (( provider_workers_maximum - count < provider_workers_preparation_calls + 1 )); then
            provider_workers_admission=capacity-exhausted
            return 0
        fi
        provider_worker_ids[$token]= provider_worker_roles[$token]=$role provider_worker_retired[$token]=false
        mkdir -m 700 -- "$provider_workers_root/receipts/$token" || return $?
        if provider_workers_retirement_requested "$token"; then
            # No create was dispatched. Preserve that same empty ID/role
            # disposition as first-pass retirement; allocate retains the role.
            provider_worker_ids[$token]= provider_worker_roles[$token]= provider_worker_retired[$token]=true
            provider_workers_publish "$token" result "retired $token" || return $?
            continue
        fi
        for other in "${!provider_worker_roles[@]}"; do
            if [[ $other != "$token" && ${provider_worker_retired[$other]} == false ]] \
                && { [[ $role == prediction && ${provider_worker_roles[$other]} == prediction ]] \
                    || [[ $role != prediction && ${provider_worker_roles[$other]} != prediction ]]; }; then
                provider_workers_diagnostic='Cannot allocate replacement native work while the previous prediction or source allocation remains owned.'
                return 2
            fi
        done
        provider_workers_create "$role" "$token" || return $?
    done
}

provider_workers_close() {
    local token
    for token in "${!provider_worker_roles[@]}"; do
        if [[ ${provider_worker_retired[$token]} != true ]]; then
            provider_workers_diagnostic='Cannot release native allocation root: exact native retirement remains unconfirmed.'
            return 2
        fi
    done
    # Caller must also have confirmed its controller stopped before this release.
    exec {provider_workers_lock}<&-
}
