#!/usr/bin/env bash
# Retire only unreferenced NMRNet images belonging to this canonical checkout.
# Docker remains the authority for container references and removal conflicts.
set -euo pipefail
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ $# == 0 ]] || { printf '%s\n' 'Use make packages/images/retire without arguments.' >&2; exit 2; }
[[ $(id -u) != 0 && $(id -g) != 0 ]] || { printf '%s\n' 'Run image retirement as the nonroot deployment owner.' >&2; exit 2; }
source "$repository_root/tools/image_lifecycle.sh"
image_lifecycle_lock exclusive
source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
docker_client_directory= scratch= inference_diagnostic=
inference_retirement_confirmed=true inference_cancellation_status=0
removed=0
finish() {
    local status=$? cleanup_status=0
    trap - EXIT
    if "$inference_retirement_confirmed"; then
        [[ -z $scratch ]] || rm -rf -- "$scratch" || cleanup_status=$?
        docker_client_cleanup || cleanup_status=$?
    else
        printf 'Image retirement has an unretired client; retain its metadata at %q and Docker client state at %q.\n' "$scratch" "$docker_client_directory" >&2
    fi
    printf '%s' "$inference_diagnostic" >&2
    [[ $status != 0 || $cleanup_status == 0 ]] || status=$cleanup_status
    if [[ $status == 0 ]]; then
        printf 'Retired %s obsolete NMRNet images; prepared, retained, container-referenced, tagged and recently created images were preserved.\n' "$removed"
    else
        printf 'Image retirement stopped after %s removals (status %s); no force or parent-image pruning was used.\n' "$removed" "$status" >&2
    fi
    exit "$status"
}
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
refuse() { printf 'Cannot retire NMRNet images: %s\n' "$1" >&2; return 2; }
docker_client_prepare run || { printf '%s\n' "$docker_client_diagnostic" >&2; exit 2; }
scratch=$(umask 077; mktemp -d /tmp/nmrnet-image-retirement.XXXXXXXX)

capture() {
    # Keep the native owner's retirement fact in this shell. Only its bounded
    # public image metadata goes to the private temporary file, never secrets.
    inference_native 'image retirement metadata' 30 stdio bash -eu -c '
        output=$1; shift
        ulimit -f 1024
        exec "$@" > "$output"
    ' bash "$scratch/output" "${engine[@]}" "$@"
    [[ $(stat -c '%s' -- "$scratch/output") -lt 1048576 ]] || refuse 'Engine metadata reached its 1 MiB limit; no complete inventory was established.'
}
require_lines() {
    local status
    if LC_ALL=C grep -aqEv "$1" "$scratch/output"; then
        refuse "$2"
    else
        status=$?
        [[ $status == 1 ]] || return "$status"
    fi
}
read_image_ids() {
    require_lines '^sha256:[0-9a-f]{64}$' 'an image inventory contained an incomplete or non-immutable image reference.'
    mapfile -t image_ids < "$scratch/output"
}
protect_output() {
    read_image_ids
    for identifier in "${image_ids[@]}"; do protected["$identifier"]=1; done
}
declare -A protected=() candidates=()
image_ids=()
require_inventory_directory() {
    local directory=$1
    if [[ -e $directory || -L $directory ]]; then
        [[ -d $directory && ! -L $directory && -r $directory && -x $directory ]] \
            || refuse "cannot read and search the inventory directory $directory; retain existing images."
    fi
}
require_inventory_directory "$repository_root/.work"
for role in provider preparation inference checker; do
    require_inventory_directory "$repository_root/.work/$role"
    record=$repository_root/.work/$role/image-id
    if [[ -e $record || -L $record ]]; then
        [[ -f $record && ! -L $record && ! -L $repository_root/.work && ! -L $repository_root/.work/$role ]] \
            || refuse 'a prepared image record is not a regular file in its original directory.'
        identifier=$(< "$record")
        [[ $identifier =~ ^sha256:[0-9a-f]{64}$ ]] && cmp -s -- "$record" <(printf '%s\n' "$identifier") \
            || refuse 'a prepared image record is malformed; keep existing images and rebuild its owner.'
        protected["$identifier"]=1
    fi
done
for directory in "$repository_root/secrets" "$repository_root/secrets/deployments"; do
    require_inventory_directory "$directory"
done
for deployment in "$repository_root/secrets/deployments/"*; do
    [[ -e $deployment || -L $deployment ]] || continue
    [[ -d $deployment && ! -L $deployment && -r $deployment && -x $deployment ]] \
        || refuse 'a deployment directory cannot be read and searched; retain existing images.'
    current=$deployment/current
    if [[ -e $current || -L $current ]]; then
        [[ -L $current ]] || refuse 'a selected deployment is not in its original directory.'
        target=$(readlink -- "$current")
        [[ $target =~ ^bundles/generation-[A-Za-z0-9_-]{1,64}$ && -d $deployment/$target && ! -L $deployment/$target \
            && -f $current/compose.json && ! -L $current/compose.json ]] || refuse 'a selected deployment recipe is missing or malformed; retain its images.'
    fi
    require_inventory_directory "$deployment/bundles"
    for bundle in "$deployment/bundles/"generation-*; do
        [[ -e $bundle || -L $bundle ]] || continue
        [[ -d $bundle && ! -L $bundle && -r $bundle && -x $bundle && -f $bundle/compose.json && ! -L $bundle/compose.json ]] \
            || refuse "generation $bundle has no readable regular compose.json; inspect it and retire unused generations with make provider/deployment/retire-generations DEPLOYMENT=<name> before retrying."
        capture compose --env-file /dev/null --project-name nmrnet-image-retirement --project-directory "$repository_root" \
            --file "$bundle/compose.json" config --no-interpolate --images
        [[ -s $scratch/output ]] || refuse 'a retained generation yielded no pinned images; retain existing images.'
        protect_output
    done
done
capture ps --all --quiet --no-trunc
if [[ -s $scratch/output ]]; then
    require_lines '^[0-9a-f]{64}$' 'the container inventory is incomplete.'
    mapfile -t containers < "$scratch/output"
    for (( offset=0; offset<${#containers[@]}; offset+=64 )); do
        batch=("${containers[@]:offset:64}")
        capture inspect --type container --format '{{.Image}}' "${batch[@]}"
        protect_output
        (( ${#image_ids[@]} == ${#batch[@]} )) || refuse 'container image inspection was incomplete.'
    done
fi
for role in provider geometry inference checker; do
    capture image ls --all --quiet --no-trunc --filter "label=io.nmrnet-repro.$role-inputs"
    read_image_ids
    for identifier in "${image_ids[@]}"; do
        [[ -v protected[$identifier] ]] || candidates["$identifier"]=1
    done
done
# Finish all inventory and alias inspection before any removal. A failed read
# must not become an empty retention set or a partially applied plan.
removable=()
now=$(date -u +%s)
[[ $now =~ ^[0-9]+$ ]] || refuse 'cannot establish the current time; retain existing images.'
for identifier in "${!candidates[@]}"; do
    capture image inspect --format '{{len .RepoTags}} {{len .RepoDigests}} {{.Created}}' "$identifier"
    aliases=$(< "$scratch/output")
    [[ $aliases =~ ^([0-9]+)\ ([0-9]+)\ ([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z)$ ]] \
        || refuse 'image alias or creation-time inspection was incomplete.'
    tags=${BASH_REMATCH[1]} digests=${BASH_REMATCH[2]} created=${BASH_REMATCH[3]}
    created_epoch=$(date -u -d "$created" +%s) || refuse 'an image creation time is invalid; retain existing images.'
    [[ $created_epoch =~ ^[0-9]+$ ]] || refuse 'an image creation time is invalid; retain existing images.'
    # Created is an extra buffer, not a publication time: cached image IDs may
    # be older than their latest use. Prepared records and recipes own pins.
    if [[ $tags == 0 && $digests == 0 ]] && (( now >= created_epoch && now - created_epoch >= 86400 )); then
        removable+=("$identifier")
    fi
done
for identifier in "${removable[@]}"; do
    # Recheck external references before deletion. Other Engine users do not
    # share this checkout lease, so these checks cannot make their actions atomic.
    capture ps --all --quiet --no-trunc --filter "ancestor=$identifier"
    [[ ! -s $scratch/output ]] || continue
    capture image inspect --format '{{len .RepoTags}} {{len .RepoDigests}}' "$identifier"
    aliases=$(< "$scratch/output")
    [[ $aliases =~ ^[0-9]+\ [0-9]+$ ]] || refuse 'image alias recheck was incomplete; retain remaining images.'
    [[ $aliases == '0 0' ]] || continue
    inference_native 'retire obsolete NMRNet image' 30 stdio "${engine[@]}" image rm --no-prune "$identifier"
    removed=$((removed + 1))
done
