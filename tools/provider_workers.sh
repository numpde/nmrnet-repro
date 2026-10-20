#!/usr/bin/env bash
# Source-only host allocation owner. Caller supplies admitted immutable images,
# source/artifact roots and finite native budgets, owns traps/controller shutdown,
# and holds this root until every native obligation is confirmed. No Job fields
# can select images, commands, paths, resources or network settings.
# Create receipts adapt worker_qualification.sh at 02a4f77:355-385; exact retirement
# stays in worker_retirement.sh. Engine calls remain serial: inference_native.sh
# retains one original native-client ownership fact across interrupted observations.

provider_workers_capture_sources() {
    local destination=$1 expected_revision=$2 commit changes source
    provider_worker_source_root= provider_worker_source_revision=
    local -a files=(tools/provider_workers.sh tools/git_inputs.sh tools/git_metadata.sh tools/container_guard.py tools/container_resources.py
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
    local root=$1 maximum=$2
    provider_workers_diagnostic=
    if [[ ! $maximum =~ ^[1-9][0-9]{0,5}$ || -L $root || ( -e $root && ! -d $root ) ]]; then
        provider_workers_diagnostic='Cannot open native allocation root: require a private directory without a symlink and an explicit positive request bound.'
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
    # These children become bind roots; the private host ancestor is not part
    # of the worker/controller namespace. Do not depend on the caller's umask.
    mkdir -m 700 -- "$root/requests" "$root/receipts" "$root/ipc" "$root/owned" || return $?
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
        --init --network none --pull never --read-only --restart no --log-driver none \
        --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus 2 --memory "$memory" --memory-swap "$memory" --pids-limit 64 \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m --workdir /workspace \
        --env NMRNET_IN_CONTAINER=1 --env PYTHONDONTWRITEBYTECODE=1 \
        "${grants[@]}" "$image" bash -eu -c \
        'umask 077; ulimit -f 64; exec "$@" > /diagnostics/stdout 2> /diagnostics/stderr' \
        bash "$interpreter" -B -m nmrnet_provider.worker_main "${arguments[@]}"; then :; else create_status=$?; fi
    if [[ $inference_retirement_confirmed == true && -f $directory/worker.cid && ! -L $directory/worker.cid ]] \
        && IFS= read -r -N 64 identifier < "$directory/worker.cid" && [[ $identifier =~ ^[0-9a-f]{64}$ ]] \
        && { cmp -s -- "$directory/worker.cid" <(printf '%s' "$identifier") \
            || cmp -s -- "$directory/worker.cid" <(printf '%s\n' "$identifier"); } \
        && cmp -s -- "$directory/create.stdout" <(printf '%s\n' "$identifier"); then
        provider_worker_ids[$token]=$identifier
    else
        provider_workers_diagnostic+='Cannot admit native create: complete response and private CID do not agree; preserve this root and original engine client. '
        return 2
    fi
    if [[ $create_status != 0 ]]; then return "$create_status"; fi
    # Retirement can arrive while create is in flight. Do not start that worker.
    if provider_workers_retirement_requested "$token"; then provider_workers_retire "$token"; return $?; fi
    provider_workers_command start "$directory" start "$identifier" || return $?
    provider_workers_command posture "$directory" inspect --type container --format \
        '{{.Id}}|{{.Image}}|{{.State.Status}}|{{.State.Running}}|{{.HostConfig.ReadonlyRootfs}}|{{.HostConfig.NetworkMode}}|{{.HostConfig.Privileged}}|{{.HostConfig.Memory}}|{{.HostConfig.MemorySwap}}|{{.HostConfig.PidsLimit}}|{{.HostConfig.NanoCpus}}|{{json .HostConfig.CapDrop}}|{{json .HostConfig.SecurityOpt}}|{{.Config.User}}|{{.HostConfig.RestartPolicy.Name}}' "$identifier" || return $?
    if ! cmp -s -- "$directory/posture.stdout" <(printf '%s|%s|running|true|true|none|false|%s|%s|64|2000000000|["ALL"]|["no-new-privileges:true"]|%s:%s|no\n' \
        "$identifier" "$image" "$memory_bytes" "$memory_bytes" "$(id -u)" "$(id -g)"); then
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

provider_workers_service() {
    local request token role count=0 candidate other
    local -a tokens=()
    for request in "$provider_workers_root/requests/"*; do
        [[ -e $request || -L $request ]] || continue
        token=${request##*/}
        count=$((count + 1))
        if [[ $count -gt $provider_workers_maximum || ! $token =~ ^[0-9a-f]{32}$ || ! -d $request || -L $request ]]; then
            provider_workers_diagnostic='Cannot service native requests: request count or private allocation directory is outside its grant.'
            return 2
        fi
        tokens+=("$token")
        [[ ${provider_worker_retired[$token]-} != true ]] || continue
        if [[ -n ${provider_worker_roles[$token]-} ]] && provider_workers_retirement_requested "$token"; then
            provider_workers_observe_final_report "$token"
            provider_workers_retire "$token" || return $?
        elif [[ -z ${provider_worker_roles[$token]-} && ! -e $request/allocate && ! -L $request/allocate ]] \
            && provider_workers_retirement_requested "$token"; then
            # Publication may have failed before the complete request appeared.
            # This sole host has never dispatched this token; late publication
            # cannot authorize a create once retirement is recorded here.
            provider_worker_ids[$token]= provider_worker_roles[$token]= provider_worker_retired[$token]=true
            mkdir -m 700 -- "$provider_workers_root/receipts/$token" || return $?
            provider_workers_publish "$token" result "retired $token" || return $?
        fi
    done
    for token in "${tokens[@]}"; do
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
        provider_worker_ids[$token]= provider_worker_roles[$token]=$role provider_worker_retired[$token]=false
        mkdir -m 700 -- "$provider_workers_root/receipts/$token" || return $?
        if provider_workers_retirement_requested "$token"; then
            provider_worker_ids[$token]= provider_worker_roles[$token]=$role provider_worker_retired[$token]=true
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
