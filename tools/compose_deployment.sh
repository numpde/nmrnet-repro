#!/usr/bin/env bash
# Provider Compose lifecycle; shared delivery has its own host-level owner.
set -euo pipefail
umask 077
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
role=${1-} action=${2-}
[[ $# == 2 && $role == provider && $action =~ ^(install|up|down|status|logs|client-inputs|retire-generations|retire-api-route)$ ]] || exit 2
caller_uid=$(id -u) caller_gid=$(id -g)
[[ $caller_uid != 0 && $caller_gid != 0 ]] || { printf '%s\n' 'Run Compose deployment as its nonroot owner.' >&2; exit 2; }
refuse() { printf 'Cannot %s %s deployment: %s\n' "$action" "$role" "$1" >&2; return 2; }
path() {
    [[ $1 == /* && $(realpath -m -- "$1") == "$1" && ! -L $1 && $1 != *','* && $1 != *$'\n'* && $1 != *$'\r'* ]] \
        || { refuse 'paths must be canonical and contain no mount delimiters or line breaks.'; return 2; }
}
private_directory() {
    path "$1"
    [[ -e $1 ]] || mkdir -m 700 -- "$1"
    [[ -d $1 && $(stat -c '%u:%a' -- "$1") == "$caller_uid:700" ]] \
        || { refuse 'configuration roots must be caller-owned mode 0700 directories.'; return 2; }
}
deployment_name=${NMRNET_DEPLOYMENT_INPUT-}
[[ $deployment_name =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$ ]] || { refuse 'supply an explicit DEPLOYMENT name.'; exit 2; }
root=$repository_root/secrets/deployments/$deployment_name
digest=$(printf '%s\0%s' "$repository_root" "$deployment_name" | sha256sum)
project=nmrnet-${digest:0:20}
project_directory=$repository_root
path "$root"
if [[ $action == install || $action == up ]]; then
    source "$repository_root/tools/image_lifecycle.sh"
    image_lifecycle_lock shared
fi
if [[ $action == install ]]; then
    mkdir -p -m 700 -- "$(dirname -- "$root")"
    private_directory "$root"
    private_directory "$root/bundles"
else
    [[ -d $root && ! -L $root ]] || { refuse 'install the configuration first.'; exit 2; }
fi
exec {deployment_lock}< "$root"
flock --nonblock "$deployment_lock" || { refuse 'another command is active; nothing was queued.'; exit 2; }
source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
docker_client_directory= inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic=
helper_name= lifetime_query= stop_query=
finish() {
    local status=$? cleanup=0 primary_diagnostic=$inference_diagnostic cleanup_diagnostic=
    trap - EXIT
    if [[ -n $helper_name && $inference_retirement_confirmed == true ]]; then
        if inference_native 'retire deployment configuration helper' 15 stdio "${engine[@]}" rm --force "$helper_name"; then
            helper_name=
        else
            cleanup=$?
        fi
        cleanup_diagnostic=$inference_diagnostic
    fi
    if [[ $inference_retirement_confirmed == true && -n ${lifetime_query-} ]]; then
        rm -f -- "$lifetime_query" || cleanup=$?
    fi
    if [[ -n $stop_query ]]; then
        if [[ $inference_retirement_confirmed == true ]]; then
            rm -f -- "$stop_query" || cleanup=$?
        else
            printf 'Deployment inspection metadata remains owned by an unretired client: %q.\n' "$stop_query" >&2
        fi
    fi
    if [[ $inference_retirement_confirmed == true ]]; then docker_client_cleanup || cleanup=$?; fi
    [[ $status != 0 || $cleanup == 0 ]] || status=$cleanup
    [[ -z $primary_diagnostic ]] || printf '%s\n' "$primary_diagnostic" >&2
    [[ -z $cleanup_diagnostic ]] || printf '%s\n' "$cleanup_diagnostic" >&2
    if [[ $status != 0 ]]; then
        printf 'Deployment command did not complete. Inspect Docker Compose status/logs and retain the selected configuration; an interrupted client does not prove Engine-side rollback. No automatic retry was sent.\n' >&2
        [[ -z $helper_name ]] || printf 'Configuration helper, if still present: %s\n' "$helper_name" >&2
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
docker_client_prepare run || { printf '%s\n' "$docker_client_diagnostic" >&2; exit 2; }
run() { local seconds=$1; shift; inference_native "$role Compose $action" "$seconds" stdio "${engine[@]}" "$@"; }
capture_stop_metadata() {
    # Preserve the native owner's facts in this shell; only bounded Engine
    # metadata goes to the private file. A timed-out writer keeps that file.
    inference_native 'provider stop inspection' 15 stdio bash -eu -c '
        output=$1; shift
        ulimit -f 4
        exec "$@" > "$output"
    ' bash "$stop_query" "${engine[@]}" "$@"
    [[ $inference_retirement_confirmed == true ]] \
        || { refuse 'provider inspection has not settled; no dependencies were stopped.'; return 2; }
    [[ $(stat -c '%s' -- "$stop_query") -lt 4096 ]] \
        || { refuse 'provider inspection reached its metadata limit; no dependencies were stopped.'; return 2; }
}
capture_inventory() {
    local operation=$1; shift
    inference_native "$operation" 30 stdio bash -eu -c '
        output=$1; shift
        ulimit -f 1024
        exec "$@" > "$output"
    ' bash "$stop_query" "${engine[@]}" "$@"
    [[ $inference_retirement_confirmed == true ]] \
        || { refuse 'deployment inventory has not settled; no owned resources were removed.'; return 2; }
    [[ $(stat -c '%s' -- "$stop_query") -lt 1048576 ]] \
        || { refuse 'deployment inventory reached its metadata limit; no owned resources were removed.'; return 2; }
}
require_no_old_route_borrowers() {
    local route_directory=$root/runtime/route-api source
    local -a containers=()
    path "$route_directory"
    stop_query=$(umask 077; mktemp /tmp/nmrnet-provider-stop.XXXXXXXX)
    capture_inventory 'old API route inventory' ps --all --quiet
    mapfile -t containers < "$stop_query"
    if (( ${#containers[@]} )); then
        capture_inventory 'old API route inventory' inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "${containers[@]}"
        while IFS= read -r source; do
            [[ -n $source ]] || continue
            source=$(realpath -m -- "$source")
            source=${source%/}
            if [[ $source == "$route_directory" || $source == "$route_directory/"* || $route_directory == "$source/"* ]]; then
                refuse 'a container still mounts the old API directory or an overlapping path; no route data was removed.'
                return 2
            fi
        done < "$stop_query"
    fi
    rm -- "$stop_query"
    stop_query=
}
provider_state() {
    capture_stop_metadata inspect --type container --format '{{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}}' "$provider_id"
    IFS=' ' read -r provider_status provider_exit provider_oom extra < "$stop_query" \
        || { refuse 'provider state is incomplete; no dependencies were stopped.'; return 2; }
    [[ $provider_status =~ ^(created|running|paused|restarting|removing|exited|dead)$ \
        && $provider_exit =~ ^[0-9]+$ && $provider_oom =~ ^(true|false)$ && -z $extra ]] \
        && cmp -s -- "$stop_query" <(printf '%s %s %s\n' "$provider_status" "$provider_exit" "$provider_oom") \
        || { refuse 'provider state is malformed; no dependencies were stopped.'; return 2; }
}
drain_provider() {
    local provider_id provider_status provider_exit provider_oom extra stop_status
    stop_query=$(umask 077; mktemp /tmp/nmrnet-provider-stop.XXXXXXXX)
    capture_stop_metadata "${compose[@]}" ps --all --quiet provider
    if [[ ! -s $stop_query ]]; then
        capture_stop_metadata "${compose[@]}" ps --all --quiet
        [[ ! -s $stop_query ]] \
            || { refuse 'the provider container is absent but dependencies remain; inspect this deployment before teardown.'; return 2; }
        return 0
    fi
    IFS= read -r provider_id < "$stop_query" \
        || { refuse 'provider selection is incomplete; no dependencies were stopped.'; return 2; }
    [[ $provider_id =~ ^[0-9a-f]{64}$ ]] \
        && cmp -s -- "$stop_query" <(printf '%s\n' "$provider_id") \
        || { refuse 'provider selection must identify exactly one container; no dependencies were stopped.'; return 2; }
    provider_state
    if [[ $provider_status == running ]]; then
        # The daemon must not turn a caller's finite wait into report loss.
        # Stop only this provider; its proxies and native owners stay available.
        if run 120 stop --signal SIGTERM --timeout -1 "$provider_id"; then :; else
            stop_status=$?
            printf 'Provider stop is not confirmed; no dependencies were stopped. Restore delivery if needed, inspect status/logs, then retry down.\n' >&2
            return "$stop_status"
        fi
        [[ $inference_retirement_confirmed == true ]] \
            || { refuse 'provider stop client has not settled; no dependencies were stopped.'; return 2; }
        provider_state
    fi
    [[ $provider_status == exited && $provider_exit == 0 && $provider_oom == false ]] \
        || { refuse "provider exit is not confirmed successful (state $provider_status, exit $provider_exit, OOM $provider_oom); no dependencies were stopped."; return 2; }
}
image_record() {
    local record=$repository_root/.work/$1/image-id
    [[ -f $record && ! -L $record && $(stat -c '%s' -- "$record") == 72 ]] || { refuse 'build or load the required immutable image first.'; return 2; }
    IFS= read -r selected_image < "$record"
    [[ $selected_image =~ ^sha256:[0-9a-f]{64}$ ]] && cmp -s "$record" <(printf '%s\n' "$selected_image") || return 2
}
select_bundle() {
    local target
    [[ -L $root/current ]] || { refuse 'install the deployment configuration first.'; return 2; }
    target=$(readlink -- "$root/current")
    [[ $target =~ ^bundles/generation-[A-Za-z0-9_-]{1,64}$ ]] || return 2
    bundle=$root/$target
    path "$bundle"
    [[ -d $bundle && -f $bundle/compose.json && ! -L $bundle/compose.json ]] || return 2
}
retire_generations() {
    local candidate source borrowed removed=0 kept=0
    local -a containers=() mounts=()
    # The deployment lock serializes supported installs/up/down. Inspect every
    # container, including stopped borrowers and mounts above or within a bundle.
    stop_query=$(umask 077; mktemp /tmp/nmrnet-provider-stop.XXXXXXXX)
    capture_inventory 'deployment generation inventory' ps --all --quiet
    mapfile -t containers < "$stop_query"
    if (( ${#containers[@]} )); then
        capture_inventory 'deployment generation inventory' inspect --format '{{range .Mounts}}{{println .Source}}{{end}}' "${containers[@]}"
        while IFS= read -r source; do
            [[ -n $source ]] || continue
            mounts+=("$(realpath -m -- "$source")")
        done < "$stop_query"
    fi
    rm -- "$stop_query"
    stop_query=
    for candidate in "$root/bundles"/generation-*; do
        [[ -d $candidate && ! -L $candidate ]] || continue
        [[ $candidate != "$bundle" ]] || { kept=$((kept + 1)); continue; }
        borrowed=false
        for source in "${mounts[@]}"; do
            source=${source%/}
            if [[ $source == "$candidate" || $source == "$candidate/"* || $candidate == "$source/"* ]]; then
                borrowed=true
                break
            fi
        done
        if [[ $borrowed == true ]]; then
            kept=$((kept + 1))
            continue
        fi
        # Sealed generations are owner-read-only. Restore directory write access
        # only inside the obsolete bundle; original inputs and runtime stay put.
        find -P "$candidate" -depth -type d -exec chmod u+rwx -- {} +
        rm -rf --one-file-system -- "$candidate"
        removed=$((removed + 1))
    done
    printf 'Retired %s unused configuration generations; retained %s selected or borrowed generations.\n' "$removed" "$kept"
}
helper() {
    local image=$1 python=$2; shift 2
    IFS= read -r helper_uuid < /proc/sys/kernel/random/uuid
    helper_name=nmrnet-config-${helper_uuid//-/}
    run 120 run --rm --pull never --name "$helper_name" --init --network none --read-only \
        --user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus 1 --memory 512m --memory-swap 512m --pids-limit 64 \
        --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=32m,mode=0700,uid=$caller_uid,gid=$caller_gid" \
        --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
        --log-opt mode=non-blocking --log-opt max-buffer-size=1m \
        "${helper_grants[@]}" --entrypoint "$python" "$image" -I -B "$@"
    helper_name=
}
select_shared_runtime() {
    shared_runtime_image=${NMRNET_SHARED_RUNTIME_IMAGE_INPUT-}
    [[ $shared_runtime_image =~ ^sha256:[0-9a-f]{64}$ ]] \
        || { refuse 'supply SHARED_RUNTIME_IMAGE=<immutable image ID> from nmr-provider-tools.'; return 2; }
    inference_native 'shared runtime image admission' 30 image-id "${engine[@]}" image inspect --format '{{.Id}}' "$shared_runtime_image"
    [[ $inference_result == "$shared_runtime_image" ]] || { refuse 'the shared runtime image identity differs from the selection.'; return 2; }
    helper_grants=(--mount "type=bind,src=$bundle,dst=$bundle")
}
initialize_lifetime() {
    image_record provider
    helper_grants=(--mount "type=bind,src=$bundle/compose.json,dst=$bundle/compose.json,readonly"
        --mount "type=bind,src=$repository_root/tools/provider_deployment.py,dst=/input/provider_deployment.py,readonly")
    local owner image volume uid gid extra
    lifetime_query=$(umask 077; mktemp /tmp/nmrnet-lifetime-query.XXXXXXXX)
    # Keep the helper in this shell so its retirement fact and container name
    # remain available to finish(); only its small stdout projection is captured.
    helper "$selected_image" /opt/provider/environment/bin/python /input/provider_deployment.py lifetime-owner --bundle "$bundle" > "$lifetime_query"
    IFS= read -r owner < "$lifetime_query"
    rm -- "$lifetime_query"
    lifetime_query=
    read -r image volume uid gid extra <<< "$owner"
    [[ $image =~ ^sha256:[0-9a-f]{64}$ && $volume =~ ^[a-z0-9][a-z0-9_-]{0,62}-control-[0-9a-f]{64}$ \
        && $uid =~ ^[1-9][0-9]*$ && $gid =~ ^[1-9][0-9]*$ && -z $extra ]] \
        || { refuse 'the installed provider lifetime grant or its image/owner is malformed.'; return 2; }
    # The project has no remaining containers at this point. A borrower from
    # any project, including a stopped container, owns the volume until removed.
    capture_stop_metadata ps --all --quiet --filter "volume=$volume"
    [[ ! -s $stop_query ]] \
        || { refuse 'the provider lifetime volume is attached to another container; inspect its owner before starting.'; return 2; }
    IFS= read -r helper_uuid < /proc/sys/kernel/random/uuid
    helper_name=nmrnet-lifetime-${helper_uuid//-/}
    run 120 run --rm --pull never --name "$helper_name" --init --network none --read-only \
        --user 0:0 --cap-drop ALL --cap-add CHOWN --security-opt no-new-privileges:true \
        --cpus 1 --memory 512m --memory-swap 512m --pids-limit 64 \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,size=32m,mode=0700 \
        --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
        --log-opt mode=non-blocking --log-opt max-buffer-size=1m \
        --mount "type=volume,src=$volume,dst=/lifetime,volume-nocopy" \
        --mount "type=bind,src=$repository_root/tools/provider_deployment.py,dst=/input/provider_deployment.py,readonly" \
        --entrypoint /opt/provider/environment/bin/python "$image" -I -B \
        /input/provider_deployment.py initialize-lifetime --uid "$uid" --gid "$gid"
    helper_name=
}
if [[ $action == install ]]; then
    # Reconfiguration is explicit; stop the existing deployment before install.
    stop_query=$(umask 077; mktemp /tmp/nmrnet-provider-stop.XXXXXXXX)
    capture_stop_metadata ps --quiet --filter "label=com.docker.compose.project=$project"
    [[ ! -s $stop_query ]] || { refuse 'stop this deployment before replacing its configuration.'; exit 2; }
    rm -- "$stop_query"
    stop_query=
    [[ ! -e $root/runtime/route-api && ! -L $root/runtime/route-api ]] \
        || { refuse 'retire the old API route with the selected old recipe before installing.'; exit 2; }
    require_no_old_route_borrowers
    image_record provider; controller_image=$selected_image
    bundle=$(mktemp -d "$root/bundles/generation-XXXXXXXX")
    select_shared_runtime
    private_directory "$root/runtime"
    for runtime_role in prediction reader smiles route-interpreter route-store; do
        private_directory "$root/runtime/$runtime_role"
    done
    # The runner borrows a stable lock inode across container replacement.
    # Create it only for a fresh native directory; never truncate or replace it.
    for runtime_role in prediction reader smiles; do
        lease=$root/runtime/$runtime_role/.owner-session.lock
        if [[ ! -e $lease && ! -L $lease ]]; then
            ( set -o noclobber; : > "$lease" ) \
                || { refuse 'cannot create the native lifetime lock exclusively.'; exit 2; }
        fi
        [[ -f $lease && ! -L $lease && $(stat -c '%u:%a' -- "$lease") == "$caller_uid:600" ]] \
            || { refuse 'native lifetime locks must be caller-owned mode 0600 regular files.'; exit 2; }
    done
    image_record inference; scientific_image=$selected_image
    image_record preparation; geometry_image=$selected_image
    source "$repository_root/tools/git_metadata.sh"
    source "$repository_root/tools/provider_image.sh"
    source "$repository_root/tools/geometry_image.sh"
    revision=$(repository_git -C "$repository_root" rev-parse --verify 'HEAD^{commit}')
    [[ $revision =~ ^[0-9a-f]{40}$ ]] || exit 2
    mapfile -t inference_selectors < "$repository_root/containers/inference/image-inputs.txt"
    selectors=("${provider_image_inputs[@]}" "${geometry_image_inputs[@]}" "${inference_selectors[@]}"
        artifacts/prediction-models.json tools/provider_deployment.py requirements/inference.lock requirements/unicore-source.json)
    mapfile -t source_names < <(repository_git -C "$repository_root" ls-tree -r --name-only HEAD -- "${selectors[@]}")
    read -r _git_mode _git_type upstream_revision _git_path < <(repository_git -C "$repository_root" ls-tree HEAD -- NMRNetplusplus)
    [[ $upstream_revision =~ ^[0-9a-f]{40}$ ]] || exit 2
    source_arguments=()
    for name in "${source_names[@]}"; do
        [[ -f $repository_root/$name && ! -L $repository_root/$name ]] || continue
        [[ $name =~ ^[A-Za-z0-9_./-]+$ && /$name/ != */../* ]] || exit 2
        helper_grants+=(--mount "type=bind,src=$repository_root/$name,dst=/source/$name,readonly")
        source_arguments+=(--source "$name")
    done
    (( ${#source_arguments[@]} > 0 )) || exit 2
    helper_grants+=(--mount "type=bind,src=$repository_root/tools/provider_deployment.py,dst=/input/provider_deployment.py,readonly")
    for configuration in provider.toml deployment.toml; do
        file=$repository_root/config/deployments/$deployment_name/$configuration
        path "$file"
        helper_grants+=(--mount "type=bind,src=$file,dst=$file,readonly")
    done
    # Mount only explicitly present flat input files, never the private root.
    for file in "$root/"*; do
        [[ -f $file && ! -L $file ]] || continue
        path "$file"
        helper_grants+=(--mount "type=bind,src=$file,dst=$file,readonly")
    done
    helper "$controller_image" /opt/provider/environment/bin/python /input/provider_deployment.py freeze \
        --source-root /source "${source_arguments[@]}" \
        --definition "$repository_root/config/deployments/$deployment_name/provider.toml" \
        --settings "$repository_root/config/deployments/$deployment_name/deployment.toml" \
        --secrets "$root" --bundle "$bundle" --models /source/artifacts/prediction-models.json \
        --deployment "$deployment_name" --checkout "$repository_root" --uid "$caller_uid" --gid "$caller_gid" \
        --shared-image-id "$shared_runtime_image" \
        --prepared "$repository_root/.work/prepared" --image-id "$scientific_image" --geometry-image-id "$geometry_image" \
        --controller-image-id "$controller_image" --repository-revision "$revision" --upstream-revision "$upstream_revision"
    [[ -f $bundle/compose.json && ! -L $bundle/compose.json ]] || exit 2
    find "$bundle" -type f -exec chmod 400 -- {} +
    find "$bundle" -mindepth 1 -type d -exec chmod 500 -- {} +
    chmod 500 "$bundle"
    # No completion/inspection manifest: selecting the actual recipe is enough.
    ln -s "bundles/${bundle##*/}" "$root/.current-${helper_uuid//-/}"
    mv -T "$root/.current-${helper_uuid//-/}" "$root/current"
    printf 'Installed %s configuration.\n' "$role"
    retire_generations
    printf 'Ensure the shared proxy and outbox are running through nmr-provider-tools.\n'
    printf 'Then: make provider/deployment/up DEPLOYMENT=%q\n' "$deployment_name"
    exit 0
fi
select_bundle
if [[ $action == retire-generations ]]; then
    retire_generations
    exit 0
fi
if [[ $action == retire-api-route ]]; then
    route_directory=$root/runtime/route-api
    require_no_old_route_borrowers
    if [[ ! -e $route_directory ]]; then
        printf 'Old API route directory is absent.\n'
        exit 0
    fi
    [[ -d $route_directory && $(stat -c '%u:%a' -- "$route_directory") == "$caller_uid:700" ]] \
        || { refuse 'the old API route must be the caller-owned private directory.'; exit 2; }
    image_record provider
    helper_grants=(--mount "type=bind,src=$bundle/compose.json,dst=$bundle/compose.json,readonly"
        --mount "type=bind,src=$repository_root/tools/provider_deployment.py,dst=/input/provider_deployment.py,readonly")
    helper "$selected_image" /opt/provider/environment/bin/python /input/provider_deployment.py old-api-route \
        --bundle "$bundle" --directory "$route_directory"
    rm -rf --one-file-system -- "$route_directory"
    printf 'Retired the old API route directory.\n'
    exit 0
fi
compose=(compose --env-file /dev/null --project-name "$project" --project-directory "$project_directory" --file "$bundle/compose.json")
case $action in
    up)
        stop_query=$(umask 077; mktemp /tmp/nmrnet-provider-stop.XXXXXXXX)
        capture_stop_metadata ps --all --quiet --filter "label=com.docker.compose.project=$project"
        [[ ! -s $stop_query ]] \
            || { refuse 'this deployment still has containers; inspect its status and use down before starting it again.'; exit 2; }
        initialize_lifetime
        run 180 "${compose[@]}" up --detach --no-build --pull never --wait --wait-timeout 120
        ;;
    down)
        drain_provider
        run 120 "${compose[@]}" down --timeout 60
        ;;
    status) run 30 "${compose[@]}" ps --all ;;
    logs) run 30 "${compose[@]}" logs --no-color --tail 80 ;;
    client-inputs)
        prediction_id=$(run 20 "${compose[@]}" ps --all --quiet prediction)
        [[ $prediction_id =~ ^[0-9a-f]{64}$ ]] || { refuse 'the selected prediction container is absent or ambiguous.'; exit 2; }
        prediction_image=$(run 20 inspect --type container --format '{{.Image}}' "$prediction_id")
        [[ $prediction_image =~ ^sha256:[0-9a-f]{64}$ ]] || exit 2
        image_record provider; controller_image=$selected_image
        [[ -n ${NMRNET_OUTPUT_INPUT-} ]] || exit 2
        path "$NMRNET_OUTPUT_INPUT"
        helper_grants=(--mount "type=bind,src=$bundle,dst=$bundle,readonly"
            --mount "type=bind,src=$repository_root/tools/provider_deployment.py,dst=/input/provider_deployment.py,readonly")
        helper "$controller_image" /opt/provider/environment/bin/python /input/provider_deployment.py client-inputs --bundle "$bundle" \
            --prediction-image-id "$prediction_image" --client-root "$NMRNET_OUTPUT_INPUT"
        ;;
esac
case $action in
    up)
        printf 'Next: make provider/deployment/status DEPLOYMENT=%q\n' "$deployment_name"
        printf 'Logs: make provider/deployment/logs DEPLOYMENT=%q\n' "$deployment_name"
        ;;
    down)
        printf 'Restart: make provider/deployment/up DEPLOYMENT=%q\n' "$deployment_name"
        printf 'Reconfigure: make provider/deployment/install DEPLOYMENT=%q SHARED_RUNTIME_IMAGE=<image ID>\n' "$deployment_name"
        ;;
esac
exit "$?"
