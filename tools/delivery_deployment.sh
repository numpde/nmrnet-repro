#!/usr/bin/env bash
# Finite owner of the host delivery service. Provider shutdown never calls this.
# The installed contained helper owns plan construction and actual-grant admission.
set -euo pipefail
umask 077
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
action=${1-}
[[ $# == 1 && $action =~ ^(install|up|status|logs|down)$ ]] || { printf '%s\n' 'Use delivery/deployment/{install,up,status,logs,down}.' >&2; exit 2; }
caller_uid=$(id -u) caller_gid=$(id -g)
[[ $caller_uid != 0 && $caller_gid != 0 ]] || { printf '%s\n' 'Run delivery deployment as its nonroot owner.' >&2; exit 2; }
root=$HOME/.local/share/nmr-api-delivery
project=nmr-api-delivery
refuse() { printf 'Cannot %s host delivery: %s\n' "$action" "$1" >&2; return 2; }
private_directory() {
    local path=$1
    [[ $path == /* && $(realpath -m -- "$path") == "$path" && ! -L $path && $path != *','* && $path != *$'\n'* && $path != *$'\r'* ]] || { refuse 'a private path is noncanonical or contains an unsupported mount delimiter.'; return 2; }
    [[ -e $path ]] || mkdir -m 700 -- "$path"
    [[ -d $path && $(stat -c '%u:%a' -- "$path") == "$caller_uid:700" ]] || { refuse 'private state directories must be caller-owned mode 0700.'; return 2; }
}
# Parents may preexist under the caller home; never chmod unrelated directories.
mkdir -p -m 700 -- "$(dirname -- "$root")"
private_directory "$root"
private_directory "$root/bundles"
exec {host_lock}< "$root"
flock --nonblock "$host_lock" || { refuse 'another host-delivery command is active; nothing was queued.'; exit 2; }
source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
docker_client_directory= inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic= pending_diagnostic=
operation= helper_id= phase=initialization
finish() {
    local status=$? cleanup=0
    trap - EXIT
    if [[ $inference_retirement_confirmed == true ]]; then docker_client_cleanup || cleanup=$?; fi
    [[ $status != 0 || $cleanup == 0 ]] || status=$cleanup
    [[ -z $pending_diagnostic ]] || printf '%s\n' "$pending_diagnostic" >&2
    [[ -z $inference_diagnostic ]] || printf '%s\n' "$inference_diagnostic" >&2
    if [[ $status != 0 ]]; then
        printf 'Host delivery did not complete phase %s. Preserve plan %s and protected receipts %s; a failed or interrupted command does not establish Engine-side rollback. No automatic retry was sent.\n' "$phase" "${bundle-unselected}" "${operation-unestablished}" >&2
        [[ -z $helper_id ]] || printf 'Original finite helper container: %s\n' "$helper_id" >&2
    elif [[ -n $operation ]]; then printf 'complete\n' > "$operation/complete"; sync -f "$operation/complete" "$operation"; fi
    exit "$status"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP
docker_client_prepare run || { printf '%s\n' "$docker_client_diagnostic" >&2; exit 2; }
if [[ $action == status || $action == logs ]]; then
    operation=$root/query
    if [[ -e $operation ]]; then
        [[ -f $operation/complete && ! -L $operation/complete ]] && cmp -s "$operation/complete" <(printf 'complete\n') \
            || { refuse 'the previous query still has unresolved local I/O; preserve its receipts before replacing it.'; exit 2; }
        rm -rf -- "$operation"
    fi
else
    operation=$root/operation
    if [[ -e $operation ]]; then
        [[ -f $operation/complete && ! -L $operation/complete ]] && cmp -s "$operation/complete" <(printf 'complete\n') \
            || { refuse 'the original command has unresolved effects; preserve operation receipts and settle its exact containers before another mutation.'; exit 2; }
        rm -rf -- "$operation"
    fi
fi
private_directory "$operation"
command_number=0
delivery_command() {
    local budget=$1; shift
    command_number=$((command_number + 1))
    local receiving=$operation/command-$command_number
    mkdir -m 700 -- "$receiving"
    printf '%s\0' "$phase" "$@" > "$receiving/intent"
    sync -f "$receiving/intent" "$receiving"
    local status=0
    if inference_native "host delivery $phase" "$budget" stdio bash -eu -c '
        receipts=$1; shift
        umask 077; ulimit -f 4096
        exec "$@" > "$receipts/stdout" 2> "$receipts/stderr"
    ' bash "$receiving" "${engine[@]}" "$@"; then :; else
        status=$?
        cat -- "$receiving/stderr" >&2 || :
        return "$status"
    fi
    command_stdout=$receiving/stdout
    command_stderr=$receiving/stderr
}
read_image() {
    local record=$1
    [[ -f $record && ! -L $record && $(stat -c '%s' -- "$record") == 72 ]] || { refuse 'an immutable image record is missing; build/load the reviewed images first.'; return 2; }
    IFS= read -r selected_image < "$record"
    [[ $selected_image =~ ^sha256:[0-9a-f]{64}$ ]] && cmp -s "$record" <(printf '%s\n' "$selected_image") || return 2
}
helper() {
    local mode=$1; shift
    local -a grants=(--mount "type=bind,src=$bundle,dst=$bundle,readonly"
        --mount "type=bind,src=$operation,dst=$operation")
    if [[ $mode == prepare ]]; then grants=(--mount "type=bind,src=$bundle,dst=$bundle" "${provider_grants[@]}"); fi
    phase=helper-$mode-create
    delivery_command 30 create --name "nmr-api-delivery-helper-${operation_generation}-$mode" \
        --label io.nmrnet-repro.delivery-helper=nmr-api-delivery \
        --network none --read-only --user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true \
        --cpus 1 --memory 256m --memory-swap 256m --pids-limit 64 --init \
        --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=0700,uid=$caller_uid,gid=$caller_gid" \
        --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
        --log-opt mode=non-blocking --log-opt max-buffer-size=1m \
        "${grants[@]}" --entrypoint /opt/provider/environment/bin/python "$controller_image" \
        -I -B -m nmrnet_provider.delivery_deployment "$mode" "$@"
    IFS= read -r helper_id < "$command_stdout"
    [[ $helper_id =~ ^[0-9a-f]{64}$ ]] && cmp -s "$command_stdout" <(printf '%s\n' "$helper_id") || return 2
    phase=helper-$mode-start; delivery_command 10 start "$helper_id"
    phase=helper-$mode-wait; delivery_command 90 wait "$helper_id"
    if ! cmp -s "$command_stdout" <(printf '0\n'); then
        phase=helper-$mode-diagnostics; delivery_command 10 logs --tail 80 "$helper_id"
        cat -- "$command_stdout" "$command_stderr" >&2
        return 2
    fi
    phase=helper-$mode-remove; delivery_command 10 rm "$helper_id"
    helper_id=
}
IFS= read -r operation_generation < /proc/sys/kernel/random/uuid
operation_generation=${operation_generation//-/}
if [[ $action == install ]]; then
    phase=existing-project
    delivery_command 15 ps --all --quiet --filter "label=com.docker.compose.project=$project"
    [[ ! -s $command_stdout ]] || { refuse 'the host delivery project still has containers; run delivery/deployment/down explicitly before reinstalling. Queue/IPC volumes are retained.'; exit 2; }
    # Use the same committed-input and immutable-image admission as normal builds.
    print_command=false
    source "$repository_root/tools/provider_image.sh"
    require_provider_image
    controller_image=$provider_image_id
    source "$repository_root/tools/delivery_image.sh"
    require_delivery_image
    delivery_image=$delivery_image_id
    [[ -n ${NMRNET_DELIVERY_DEPLOYMENTS_INPUT-} ]] || { refuse 'supply explicit DEPLOYMENTS profile names.'; exit 2; }
    read -r -a profiles <<< "$NMRNET_DELIVERY_DEPLOYMENTS_INPUT"
    (( ${#profiles[@]} > 0 && ${#profiles[@]} <= 16 )) || exit 2
    declare -A selected_profiles=()
    provider_grants=() provider_arguments=()
    for profile in "${profiles[@]}"; do
        [[ $profile =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$ && ! -v selected_profiles[$profile] ]] || { refuse 'profile names must be distinct literal deployment selectors.'; exit 2; }
        selected_profiles[$profile]=true
        original=$repository_root/secrets/deployments/$profile
        [[ -L $original/current ]] || exit 2
        target=$(readlink -- "$original/current")
        [[ $target =~ ^bundles/generation-[A-Za-z0-9_-]{1,64}$ ]] || exit 2
        selected=$original/$target
        [[ $(realpath -e -- "$selected") == "$selected" && ! -L $selected && $(stat -c '%u:%a' -- "$selected") == "$caller_uid:500" ]] || exit 2
        provider_grants+=(--mount "type=bind,src=$selected,dst=$selected,readonly")
        provider_arguments+=(--provider-bundle "$selected")
    done
    bundle=$(mktemp -d "$root/bundles/generation-XXXXXXXX")
    helper prepare "${provider_arguments[@]}" --output "$bundle" --uid "$caller_uid" --gid "$caller_gid" \
        --delivery-image "$delivery_image" --controller-image "$controller_image"
    printf '%s\n' "$controller_image" > "$bundle/controller-image-id"
    printf '%s\n' "$delivery_image" > "$bundle/delivery-image-id"
    [[ -f $bundle/compose.json && ! -L $bundle/compose.json && -f $bundle/image-ids && -f $bundle/volume-names ]] || exit 2
    find "$bundle/bindings" -type f -exec chmod 400 -- {} +
    find "$bundle/bindings" -type d -exec chmod 500 -- {} +
    chmod 400 "$bundle/compose.json" "$bundle/daemon.toml" "$bundle/controller-image-id" "$bundle/delivery-image-id" "$bundle/image-ids" "$bundle/volume-names"
    printf 'complete\n' > "$bundle/complete"; chmod 400 "$bundle/complete"
    sync -f "$bundle/complete" "$bundle"
    chmod 500 "$bundle"
    ln -s "bundles/${bundle##*/}" "$root/.current-$operation_generation"
    mv -T "$root/.current-$operation_generation" "$root/current"
    sync -f "$root"
    printf 'Installed host delivery %s; no runtime containers were started.\n' "${bundle##*/}"
    exit 0
fi
[[ -L $root/current ]] || { refuse 'install the host delivery inputs first.'; exit 2; }
target=$(readlink -- "$root/current")
[[ $target =~ ^bundles/generation-[A-Za-z0-9_-]{1,64}$ ]] || exit 2
bundle=$root/$target
[[ ! -L $bundle && $(stat -c '%u:%a' -- "$bundle") == "$caller_uid:500" ]] && cmp -s "$bundle/complete" <(printf 'complete\n') || exit 2
read_image "$bundle/controller-image-id"; controller_image=$selected_image
read_image "$bundle/delivery-image-id"; delivery_image=$selected_image
compose=(compose --env-file /dev/null --project-name "$project" --project-directory "$bundle" --file "$bundle/compose.json")
# Admission consumes Engine observations inside the installed controller image;
# the host never interprets arbitrary JSON. Its output is not a new runtime plan.
admit() {
    phase=inventory; delivery_command 15 ps --all --no-trunc --quiet --filter "label=com.docker.compose.project=$project"
    mapfile -t identifiers < "$command_stdout"
    (( ${#identifiers[@]} > 0 && ${#identifiers[@]} <= 18 )) || return 2
    for identifier in "${identifiers[@]}"; do [[ $identifier =~ ^[0-9a-f]{64}$ ]] || return 2; done
    phase=container-inspection; delivery_command 30 inspect --type container "${identifiers[@]}"
    cp -- "$command_stdout" "$operation/containers.json"
    mapfile -t image_ids < "$bundle/image-ids"
    for identifier in "${image_ids[@]}"; do [[ $identifier =~ ^sha256:[0-9a-f]{64}$ ]] || return 2; done
    (( ${#image_ids[@]} > 0 && ${#image_ids[@]} <= 2 )) || return 2
    phase=image-inspection; delivery_command 30 image inspect "${image_ids[@]}"
    cp -- "$command_stdout" "$operation/images.json"
    [[ ! -e $operation/admitted-ids ]] || rm -- "$operation/admitted-ids"
    helper admit --plan "$bundle/compose.json" --inspection "$operation/containers.json" --images "$operation/images.json" --output "$operation/admitted-ids"
}
case $action in
    up)
        phase=existing-project
        delivery_command 15 ps --all --no-trunc --quiet --filter "label=com.docker.compose.project=$project"
        if [[ -s $command_stdout ]]; then admit; fi
        # External stable volumes are explicit plan-owned names, not discovered
        # from other projects. Initialization remains a service in the recipe.
        while IFS= read -r volume; do
            [[ $volume == nmr-api-delivery-queue || $volume =~ ^nmr-api-delivery-(ipc|control)-[0-9a-f]{64}$ ]] || exit 2
            phase=volume-create; delivery_command 15 volume create "$volume"
        done < "$bundle/volume-names"
        phase=compose-create; delivery_command 60 "${compose[@]}" create --no-build --pull never --no-recreate
        admit
        phase=compose-up; delivery_command 120 "${compose[@]}" up --no-build --pull never --no-recreate --detach --wait --wait-timeout 90
        admit
        printf 'Host delivery is running from %s. API publication remains independently observable through delivery status.\n' "${bundle##*/}"
        ;;
    status)
        phase=compose-status; delivery_command 20 "${compose[@]}" ps --all
        cat -- "$command_stdout"
        ;;
    logs)
        phase=compose-logs; delivery_command 30 "${compose[@]}" logs --no-color --tail 80
        cat -- "$command_stdout" "$command_stderr"
        ;;
    down)
        # Never delete queue/IPC volumes. This is independent of provider down.
        admit
        phase=compose-down; delivery_command 90 "${compose[@]}" down --timeout 60
        printf 'Host delivery containers stopped and removed; queue and IPC volumes retained.\n'
        ;;
esac
