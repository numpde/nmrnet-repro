#!/usr/bin/env bash
# Own only the shared services created for one disposable Compose fixture.
set -euo pipefail
umask 077
[[ $# == 5 ]] || exit 2
action=$1 scratch=$2 project=$3 image=$4 root=$5
[[ $action == preflight || $action == capture || $action == retire ]] || exit 2
[[ $scratch =~ ^/tmp/nmrnet-compose-[A-Za-z0-9]{8}$ && -d $scratch && ! -L $scratch ]] || exit 2
[[ $(stat -c '%u:%a' -- "$scratch") == "$(id -u):700" ]] || exit 2
digest=$(printf '%s' "$scratch" | sha256sum)
[[ $project == "nmrnet-qualification-${digest:0:24}" && $image =~ ^sha256:[0-9a-f]{64}$ ]] || exit 2
[[ $(id -u) != 0 && $(id -g) != 0 ]] || exit 2
source "$root/tools/checker_operation.sh"
checker_begin
mutation_requested=false
finish() {
    local status=$?
    if (( status != 0 )) && "$mutation_requested"; then
        trap - EXIT
        printf 'Fixture retirement may have partially completed; retained reservation and operation %q, client %q and fixture %q. Inspect and reconcile these resources before any new operation.\n' \
            "$operation_directory" "$docker_client_directory" "$scratch" >&2
        [[ -z $operation_diagnostic ]] || printf '%s' "$operation_diagnostic" >&2
        exit "$status"
    fi
    (exit "$status") || checker_finish
    checker_finish
}
trap finish EXIT
fail() { printf 'Cannot %s disposable shared fixture: %s\n' "$action" "$1" >&2; exit "${2:-2}"; }
checker_metadata "$operation_directory/image" image inspect --format '{{.Id}} {{index .Config.Labels "org.nmr-provider-tools.role"}} {{index .Config.Labels "org.nmr-provider-tools.inputs"}}' "$image" || fail 'image inspection failed.'
IFS= read -r observed < "$operation_directory/image"
[[ $observed =~ ^(sha256:[0-9a-f]{64})\ delivery\ ([0-9a-f]{64})$ && ${BASH_REMATCH[1]} == "$image" ]] \
    && cmp -s "$operation_directory/image" <(printf '%s\n' "$observed") || fail 'image is not an admitted shared delivery build.'
checker_create --name "nmr-provider-tools-operation-$project" --pull never --network none --read-only \
    --user "$(id -u):$(id -g)" --cap-drop ALL --security-opt no-new-privileges:true --cpus 1 \
    --memory 64m --memory-swap 64m --pids-limit 16 --entrypoint /bin/true "$image" || fail 'project reservation is unavailable.'

receipt_dir=$scratch/delivery
[[ -d $receipt_dir && ! -L $receipt_dir && $(stat -c '%u:%a' -- "$receipt_dir") == "$(id -u):700" ]] || fail 'fixture receipt directory is not private.'
proxy_project=$project-proxy
proxy_volume=$project-proxy-socket
intake_volume=$project-outbox-socket
if [[ $action == preflight ]]; then
    for owner in "$project" "$proxy_project"; do
        checker_metadata "$operation_directory/existing" ps --all --quiet --no-trunc --filter "label=com.docker.compose.project=$owner" || fail 'project census failed.'
        [[ ! -s $operation_directory/existing ]] || fail 'fixture project already has containers.'
    done
    for volume in "$proxy_volume" "$intake_volume" "$project-queue"; do
        checker_metadata "$operation_directory/existing" volume ls --filter "name=^${volume}$" --format '{{.Name}}' || fail 'volume census failed.'
        [[ ! -s $operation_directory/existing ]] || fail 'fixture volume already exists.'
    done
    exit 0
fi
container_inventory() {
    checker_metadata "$operation_directory/containers" ps --all --no-trunc --quiet --filter "label=com.docker.compose.project=$proxy_project" || return $?
    mapfile -t ids < "$operation_directory/containers"
    (( ${#ids[@]} > 0 && ${#ids[@]} <= 2 )) || return 2
    cmp -s "$operation_directory/containers" <(printf '%s\n' "${ids[@]}") || return 2
    local id kind observed
    : > "$operation_directory/proxy-receipt"
    for id in "${ids[@]}"; do
        [[ $id =~ ^[0-9a-f]{64}$ ]] || return 2
        checker_metadata "$operation_directory/container" inspect --type container --format '{{.Id}}|{{.Image}}|{{index .Config.Labels "org.nmr-provider-tools.delivery"}}|{{index .Config.Labels "com.docker.compose.project"}}|{{index .Config.Labels "com.docker.compose.service"}}|{{range .Mounts}}{{if eq .Destination "/route"}}{{.Name}}{{end}}{{end}}|{{range .Mounts}}{{if eq .Destination "/settings"}}{{.Source}}{{end}}{{end}}' "$id" || return $?
        IFS= read -r observed < "$operation_directory/container"
        kind=${observed#*|*|*|*|}; kind=${kind%%|*}
        case $kind in
            proxy) expected_settings=$scratch/data/shared-settings ;;
            init) expected_settings= ;;
            *) return 2 ;;
        esac
        cmp -s "$operation_directory/container" <(printf '%s|%s|%s|%s|%s|%s|%s\n' "$id" "$image" "$proxy_project" "$proxy_project" "$kind" "$proxy_volume" "$expected_settings") || return 2
        cat "$operation_directory/container" >> "$operation_directory/proxy-receipt"
    done
    sort -o "$operation_directory/proxy-receipt" "$operation_directory/proxy-receipt"
    [[ $(grep -c '|proxy|' "$operation_directory/proxy-receipt") == 1 ]] || return 2
}
volume_receipt() {
    local volume=$1 owner=$2 destination=$3 observed
    checker_metadata "$destination" volume inspect --format '{{.Name}}|{{index .Labels "com.docker.compose.project"}}|{{index .Labels "com.docker.compose.volume"}}|{{.Driver}}|{{json .Options}}|{{.CreatedAt}}' "$volume" || return $?
    IFS= read -r observed < "$destination"
    [[ $observed == "$volume|$owner|$volume|local|null|"* || $observed == "$volume|$owner|$volume|local|{}|"* ]] || return 2
    [[ ${observed##*|} != '' ]] && cmp -s "$destination" <(printf '%s\n' "$observed")
}
container_inventory || fail 'proxy identity or mounts could not be confirmed.'
volume_receipt "$proxy_volume" "$proxy_project" "$operation_directory/proxy-volume" || fail 'proxy socket ownership could not be confirmed.'
volume_receipt "$intake_volume" "$project" "$operation_directory/intake-volume" || fail 'outbox socket ownership could not be confirmed.'
if [[ $action == capture ]]; then
    for name in proxy-receipt proxy-volume intake-volume; do
        [[ ! -e $receipt_dir/$name && ! -L $receipt_dir/$name ]] || fail 'original fixture receipts already exist.'
        cp -- "$operation_directory/$name" "$receipt_dir/$name"
    done
    exit 0
fi
for name in proxy-receipt proxy-volume intake-volume; do
    [[ -f $receipt_dir/$name && ! -L $receipt_dir/$name ]] && cmp -s "$receipt_dir/$name" "$operation_directory/$name" || fail 'fixture identity changed since startup.'
done
checker_metadata "$operation_directory/outbox-containers" ps --all --quiet --no-trunc --filter "label=com.docker.compose.project=$project" || fail 'outbox container census failed.'
[[ ! -s $operation_directory/outbox-containers ]] || fail 'outbox containers remain.'
checker_metadata "$operation_directory/borrowers" ps --all --quiet --no-trunc --filter "volume=$proxy_volume" || fail 'proxy borrower census failed.'
sort "$operation_directory/borrowers" > "$operation_directory/borrowers-sorted"
printf '%s\n' "${ids[@]}" | sort > "$operation_directory/ids-sorted"
cmp -s "$operation_directory/borrowers-sorted" "$operation_directory/ids-sorted" || fail 'proxy socket has an unexpected borrower.'
checker_metadata "$operation_directory/intake-borrowers" ps --all --quiet --no-trunc --filter "volume=$intake_volume" || fail 'outbox borrower census failed.'
[[ ! -s $operation_directory/intake-borrowers ]] || fail 'outbox socket still has borrowers.'
mutation_requested=true
for id in "${ids[@]}"; do
    checker_call 'stop disposable proxy container' 60 stdio "${engine[@]}" stop --time 15 "$id" || { status=$?; fail 'proxy container stop was not confirmed.' "$status"; }
    checker_metadata "$operation_directory/state" inspect --type container --format '{{.State.Status}}|{{.State.Running}}|{{.State.Pid}}|{{.State.Restarting}}' "$id" || fail 'proxy container state could not be inspected.'
    cmp -s "$operation_directory/state" <(printf 'exited|false|0|false\n') || fail 'proxy container has not stopped.'
    checker_call 'remove disposable proxy container' 60 stdio "${engine[@]}" rm "$id" || { status=$?; fail 'proxy container removal was not confirmed.' "$status"; }
done
checker_metadata "$operation_directory/remaining" ps --all --quiet --no-trunc --filter "label=com.docker.compose.project=$proxy_project" || fail 'proxy census failed after removal.'
[[ ! -s $operation_directory/remaining ]] || fail 'proxy containers remain.'
for spec in "$proxy_volume:$proxy_project:proxy-volume" "$intake_volume:$project:intake-volume"; do
    IFS=: read -r volume owner name <<< "$spec"
    volume_receipt "$volume" "$owner" "$operation_directory/recheck" || fail 'socket volume reinspection failed.'
    cmp -s "$receipt_dir/$name" "$operation_directory/recheck" || fail 'socket volume changed.'
    checker_metadata "$operation_directory/borrowers" ps --all --quiet --no-trunc --filter "volume=$volume" || fail 'socket borrower reinspection failed.'
    [[ ! -s $operation_directory/borrowers ]] || fail 'socket still has borrowers.'
    checker_call 'retire disposable socket volume' 60 stdio "${engine[@]}" volume rm -- "$volume" || { status=$?; fail 'socket removal was not confirmed.' "$status"; }
done
for volume in "$proxy_volume" "$intake_volume"; do
    checker_metadata "$operation_directory/remaining-volume" volume ls --filter "name=^${volume}$" --format '{{.Name}}' || fail 'final socket census failed.'
    [[ ! -s $operation_directory/remaining-volume ]] || fail 'a fixture socket volume remains.'
done
printf 'Disposable proxy and socket volumes retired; the fixture queue remains.\n'
