#!/usr/bin/env bash
# Host sequencing only: existing owners retain downloads, image admission and
# cleanup. The cooperative controller wait preserves those owners on signals.
set -euo pipefail
repository_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
print_command=false
if [[ $# == 1 && $1 == --print ]]; then print_command=true; shift; fi
if [[ $# != 0 ]]; then printf '%s\n' 'Use make provider/prepare with optional WHEELHOUSE and NETWORK_INTERFACE selections.' >&2; exit 2; fi
source "$repository_root/tools/wheelhouse_path.sh"
select_wheelhouse
source "$repository_root/tools/acquisition_interface.sh"
select_acquisition_interface
source "$repository_root/tools/inference_native.sh"
inference_diagnostic= inference_cancellation_status=0 inference_retirement_confirmed=true caller_cancellation=0
finish() {
    local status=$?
    trap - EXIT
    if [[ $caller_cancellation != 0 ]]; then exit "$caller_cancellation"; fi
    if [[ $inference_cancellation_status != 0 ]]; then exit "$inference_cancellation_status"; fi
    # This wrapper owns no engine grants. Never replace the child's failure or
    # guess at its cleanup from text; return its cooperative completion status.
    if printf '%s' "$inference_diagnostic" >&2; then :; fi
    exit "$status"
}
trap finish EXIT
trap 'caller_cancellation=129; exit 129' HUP
trap 'caller_cancellation=130; exit 130' INT
trap 'caller_cancellation=143; exit 143' TERM

prepare_step() {
    local purpose=$1
    local target=$2 owner action archive=
    shift 2
    case $target in
        packages/wheelhouse) owner=wheelhouse.sh; action=download ;;
        packages/image) owner=inference_image.sh; action=build ;;
        packages/import) owner=inference_image.sh; action=import ;;
        geometry/image/build|provider/image/build) owner=container.sh; action=$target ;;
        weights/download) owner=container.sh; action=download ;;
        weights/extract) owner=container.sh; action=extract ;;
        *) printf 'Cannot prepare provider: unsupported owned step %q.\n' "$target" >&2; return 2 ;;
    esac
    local selection
    for selection in "$@"; do
        case $selection in ARCHIVE=*) archive=${selection#ARCHIVE=} ;; esac
    done
    # Invoke each established owner directly so this Make operation's selected
    # endpoint remains in-process and no child Make can inherit ambient grants.
    local -a command=(env
        NMRNET_SUPPRESS_NEXT_ACTIONS=1
        "NMRNET_WHEELHOUSE_INPUT=$wheelhouse_directory" NMRNET_WHEELHOUSE_INPUT_ORIGIN='command line'
        "NMRNET_NETWORK_INTERFACE_INPUT=$interface" NMRNET_NETWORK_INTERFACE_INPUT_ORIGIN='command line'
        "NMRNET_ARCHIVE_INPUT=$archive"
        "NMRNET_DOCKER_CONTEXT_INPUT=${NMRNET_DOCKER_CONTEXT_INPUT-}"
        NMRNET_DOCKER_CONTEXT_INPUT_ORIGIN='command line'
        "NMRNET_DOCKER_ENDPOINT_INPUT=${NMRNET_DOCKER_ENDPOINT_INPUT-}"
        bash "$repository_root/tools/$owner")
    command+=("$action")
    if "$print_command"; then
        command=("${command[@]:0:${#command[@]}-1}" --print "${command[@]: -1}")
        printf '%q ' "${command[@]}"; printf '\n'
    else
        printf '\n%s\n' "$purpose" >&2
        controller_native "provider preparation: $target" untimed stdio "${command[@]}"
    fi
}

prepare_models() {
    local archive=$1
    local prepared=$repository_root/.work/prepared/${archive%.zip}
    if [[ -e $prepared || -L $prepared ]]; then
        # This decides whether creation is needed, not whether model bytes are
        # admitted. The prediction owner still checks the five selected inputs
        # against prediction-models.json before decoding them.
        if [[ ! -d $prepared || -L $prepared || ! -f $prepared/provenance.json || ! -s $prepared/provenance.json || -L $prepared/provenance.json \
            || $(realpath -m -- "$prepared") != "$prepared" ]]; then
            printf 'Cannot reuse %q: require an unlinked extraction directory with a nonempty regular provenance.json. Existing files were preserved.\n' "$prepared" >&2
            return 2
        fi
        if "$print_command"; then printf '# Keep existing extraction at %q; model bytes are checked before use.\n' "$prepared"
        else printf 'Keeping existing extraction at %s; the provider checks selected files against pinned hashes before decoding.\n' "$prepared" >&2; fi
        return
    fi
    # An existing archive goes straight to extraction's byte verification;
    # absence alone requires acquisition. A partial download resumes there.
    if [[ ! -e $repository_root/.work/downloads/$archive && ! -L $repository_root/.work/downloads/$archive ]]; then
        prepare_step "Acquire $archive in $repository_root/.work/downloads/" weights/download "ARCHIVE=$archive"
    fi
    prepare_step "Extract $archive" weights/extract "ARCHIVE=$archive"
    if ! "$print_command"; then printf 'Extracted model files to %s/\n' "$prepared" >&2; fi
}

# Parse the whole sequence before waiting for a long-running child, as the
# service launcher does; exit afterward without reading a changed script tail.
prepare_provider() {
    prepare_step 'Acquire all package dependencies' packages/wheelhouse
    prepare_step 'Build the inference image' packages/image
    prepare_step 'Check inference imports' packages/import
    prepare_step 'Build the geometry image' geometry/image/build
    prepare_step 'Build the controller image' provider/image/build
    prepare_models weight.zip
    prepare_models checkpoints.zip
    if ! "$print_command"; then
        if controller_native 'provider preparation: image retirement' untimed stdio \
            bash "$repository_root/tools/image_retirement.sh"; then :; else
            status=$?
            if ! "$inference_retirement_confirmed" || [[ $inference_cancellation_status != 0 ]]; then return "$status"; fi
            printf 'Provider preparation succeeded; image retirement is incomplete (status %s). Retry make packages/images/retire.\n' "$status" >&2
        fi
        printf '\n%s\n' 'Images and models are ready. Next, configure credentials and the named deployment:' \
            '  https://numpde.github.io/nmrnet-repro/provider.html#provider-startup-configuration' >&2
    fi
}
{
    if ! "$print_command"; then printf 'Cache directory: %s\nNetwork interface: %s\n' "$wheelhouse_directory" "$interface" >&2; fi
    prepare_provider
    exit "$?"
}
