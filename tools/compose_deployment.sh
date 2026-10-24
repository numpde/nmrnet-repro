#!/usr/bin/env bash
# Two concrete Compose deployments; actual configuration is the only host state.
set -euo pipefail
umask 077
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
role=${1-} action=${2-}
[[ $# == 2 && $role =~ ^(provider|delivery)$ && $action =~ ^(install|up|down|status|logs|client-inputs|discard|held)$ ]] || exit 2
[[ $role == provider || $action != client-inputs ]] || exit 2
[[ $role == delivery || ( $action != discard && $action != held ) ]] || exit 2
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
if [[ $role == provider ]]; then
    deployment_name=${NMRNET_DEPLOYMENT_INPUT-}
    [[ $deployment_name =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$ ]] || { refuse 'supply an explicit DEPLOYMENT name.'; exit 2; }
    root=$repository_root/secrets/deployments/$deployment_name
    digest=$(printf '%s\0%s' "$repository_root" "$deployment_name" | sha256sum)
    project=nmrnet-${digest:0:20}
    project_directory=$repository_root
else
    root=$HOME/.local/share/nmr-api-delivery
    project=nmr-api-delivery
    project_directory=$root
fi
path "$root"
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
helper_name=
finish() {
    local status=$? cleanup=0
    trap - EXIT
    if [[ -n $helper_name && $inference_retirement_confirmed == true ]]; then
        inference_native 'retire deployment configuration helper' 15 stdio "${engine[@]}" rm --force "$helper_name" || cleanup=$?
    fi
    if [[ $inference_retirement_confirmed == true ]]; then docker_client_cleanup || cleanup=$?; fi
    [[ $status != 0 || $cleanup == 0 ]] || status=$cleanup
    [[ -z $inference_diagnostic ]] || printf '%s\n' "$inference_diagnostic" >&2
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
if [[ $action == install ]]; then
    # Reconfiguration is explicit; stop the existing deployment before install.
    existing=$(run 15 ps --quiet --filter "label=com.docker.compose.project=$project")
    [[ -z $existing ]] || { refuse 'stop this deployment before replacing its configuration.'; exit 2; }
    image_record provider; controller_image=$selected_image
    bundle=$(mktemp -d "$root/bundles/generation-XXXXXXXX")
    helper_grants=(--mount "type=bind,src=$bundle,dst=$bundle")
    if [[ $role == provider ]]; then
        private_directory "$root/runtime"
        for runtime_role in prediction reader smiles route-api route-interpreter route-store; do
            private_directory "$root/runtime/$runtime_role"
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
            --prepared "$repository_root/.work/prepared" --image-id "$scientific_image" --geometry-image-id "$geometry_image" \
            --controller-image-id "$controller_image" --repository-revision "$revision" --upstream-revision "$upstream_revision"
    else
        image_record delivery; delivery_image=$selected_image
        [[ -n ${NMRNET_DELIVERY_DEPLOYMENTS_INPUT-} && $NMRNET_DELIVERY_DEPLOYMENTS_INPUT != *$'\n'* && $NMRNET_DELIVERY_DEPLOYMENTS_INPUT != *$'\r'* ]] \
            || { refuse 'supply explicit space-separated DEPLOYMENTS profile names.'; exit 2; }
        read -r -a profiles <<< "$NMRNET_DELIVERY_DEPLOYMENTS_INPUT"
        declare -A seen_profiles=()
        provider_arguments=()
        for profile in "${profiles[@]}"; do
            [[ $profile =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$ && ! -v seen_profiles[$profile] ]] || exit 2
            seen_profiles[$profile]=true
            original=$repository_root/secrets/deployments/$profile
            [[ -L $original/current ]] || exit 2
            target=$(readlink -- "$original/current")
            [[ $target =~ ^bundles/generation-[A-Za-z0-9_-]{1,64}$ ]] || exit 2
            provider_bundle=$original/$target
            path "$provider_bundle"
            helper_grants+=(--mount "type=bind,src=$provider_bundle,dst=$provider_bundle,readonly")
            provider_arguments+=(--provider-bundle "$provider_bundle")
        done
        (( ${#profiles[@]} > 0 && ${#profiles[@]} <= 16 )) || exit 2
        helper "$controller_image" /opt/provider/environment/bin/python -m nmrnet_provider.delivery_deployment prepare "${provider_arguments[@]}" \
            --output "$bundle" --uid "$caller_uid" --gid "$caller_gid" \
            --delivery-image "$delivery_image" --controller-image "$controller_image"
    fi
    [[ -f $bundle/compose.json && ! -L $bundle/compose.json ]] || exit 2
    if [[ $role == provider ]]; then
        find "$bundle" -type f -exec chmod 400 -- {} +
        find "$bundle" -mindepth 1 -type d -exec chmod 500 -- {} +
    else
        find "$bundle/bindings" -type f -exec chmod 400 -- {} +
        find "$bundle/bindings" -type d -exec chmod 500 -- {} +
        chmod 400 "$bundle/compose.json" "$bundle/daemon.toml"
    fi
    chmod 500 "$bundle"
    # No completion/inspection manifest: selecting the actual recipe is enough.
    ln -s "bundles/${bundle##*/}" "$root/.current-${helper_uuid//-/}"
    mv -T "$root/.current-${helper_uuid//-/}" "$root/current"
    printf 'Installed %s configuration.\n' "$role"
    if [[ $role == provider ]]; then
        printf 'Next: make delivery/deployment/install DEPLOYMENTS=%q\n' "$deployment_name"
        printf 'Then: make delivery/deployment/up\n'
        printf 'Then: make provider/deployment/up DEPLOYMENT=%q\n' "$deployment_name"
    else
        printf 'Next: make delivery/deployment/up\n'
    fi
    exit 0
fi
select_bundle
compose=(compose --env-file /dev/null --project-name "$project" --project-directory "$project_directory" --file "$bundle/compose.json")
case $action in
    discard|held)
        required_selectors=(API_ORIGIN PROVIDER_REF)
        if [[ $action == discard ]]; then required_selectors+=(EXECUTION_ATTEMPT_REF BODY_SHA256); fi
        for selector in "${required_selectors[@]}"; do
            alias=NMRNET_${selector}_INPUT
            [[ -n ${!alias-} ]] || { refuse "supply explicit $selector from held-item metadata; stop shared delivery first."; exit 2; }
        done
        image_record delivery
        helper_grants=(--mount "type=bind,src=$bundle/daemon.toml,dst=/input/daemon.toml,readonly"
            --mount 'type=volume,src=nmr-api-delivery-queue,dst=/state,volume-nocopy')
        operator_arguments=(--list-held)
        if [[ $action == discard ]]; then
            operator_arguments=(--execution-attempt-ref "$NMRNET_EXECUTION_ATTEMPT_REF_INPUT" --body-sha256 "$NMRNET_BODY_SHA256_INPUT")
        fi
        helper "$selected_image" /opt/provider-core/environment/bin/python -m provider_core.delivery_discard \
            --config /input/daemon.toml --api-origin "$NMRNET_API_ORIGIN_INPUT" \
            --provider-ref "$NMRNET_PROVIDER_REF_INPUT" "${operator_arguments[@]}"
        ;;
    up) run 180 "${compose[@]}" up --detach --no-build --pull never --wait --wait-timeout 120 ;;
    down) run 120 "${compose[@]}" down --timeout 60 ;;
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
        if [[ $role == provider ]]; then
            printf 'Next: make provider/deployment/status DEPLOYMENT=%q\n' "$deployment_name"
            printf 'Logs: make provider/deployment/logs DEPLOYMENT=%q\n' "$deployment_name"
        else
            printf 'Next: make delivery/deployment/status\nLogs: make delivery/deployment/logs\n'
        fi
        ;;
    down)
        if [[ $role == provider ]]; then
            printf 'Restart: make provider/deployment/up DEPLOYMENT=%q\n' "$deployment_name"
            printf 'Reconfigure: make provider/deployment/install DEPLOYMENT=%q\n' "$deployment_name"
        else
            printf 'Restart: make delivery/deployment/up\nReconfigure: make delivery/deployment/install DEPLOYMENTS="<profile names>"\n'
        fi
        ;;
    discard) printf 'Next: make delivery/deployment/up\n' ;;
    held) printf 'Next: reconcile any held item before discard; otherwise make delivery/deployment/up\n' ;;
esac
