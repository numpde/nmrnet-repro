#!/usr/bin/env bash
# Host orchestration only; repository Python runs in explicit contained lanes.
set -euo pipefail

readonly image='docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c'
readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly caller_uid="$(id -u)"
readonly caller_gid="$(id -g)"

print_command=false
if [[ $# -gt 0 && "$1" == --print ]]; then
    print_command=true
    shift
fi
if [[ $# -ne 1 ]]; then
    printf '%s\n' 'Usage: bash tools/container.sh [--print] LANE; see make help for contained test, provider-core, repository and artifact lanes.' >&2
    exit 2
fi

source "$repository_root/tools/docker_client.sh"
source "$repository_root/tools/inference_native.sh"
next_actions=

prepare_directory() {
    local directory="$1" preparation_status
    if [[ -L "$directory" || ( -e "$directory" && ! -d "$directory" ) ]]; then
        printf -v pending_diagnostic '%sCannot use artifact directory; expected a directory without a symlink: %s\n' "$pending_diagnostic" "$directory"
        exit 2
    fi
    if ! "$print_command"; then
        if mkdir -p -- "$directory" 2>/dev/null; then :; else
            preparation_status=$?
            printf -v pending_diagnostic '%sCannot prepare artifact directory at %q (status %s).\n' "$pending_diagnostic" "$directory" "$preparation_status"
            exit "$preparation_status"
        fi
    fi
}

require_source_directory() {
    if [[ ! -d "$repository_root/$1" || -L "$repository_root/$1" ]]; then
        printf -v pending_diagnostic '%sCannot run contained checks: %s must be a directory without a symlink.\n' "$pending_diagnostic" "$1"
        exit 2
    fi
}

image_input_directory=
check_input_directory=
probe_directory=
docker_client_directory=
pending_diagnostic=
inference_diagnostic=
inference_retirement_confirmed=true
wheel_capture_retirement_confirmed=true
inference_cancellation_status=0
caller_cancellation=0
network=none

checker_image() {
    local record="$repository_root/.work/checker/image-id" inspect_status record_size metadata_status
    if "$print_command"; then
        checker_image_id=CHECKER_IMAGE_ID_FROM_RECORD
        return
    fi
    for directory in "$repository_root/.work" "$repository_root/.work/checker"; do
        if [[ ! -d "$directory" || -L "$directory" ]]; then
            printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot run repository checks: checker record directories must exist without symlinks.'
            exit 2
        fi
    done
    if [[ ! -f "$record" || -L "$record" ]]; then
        printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot run repository checks: image-id must contain exactly one full immutable image ID and a newline; prepare checks/image/build explicitly.'
        exit 2
    fi
    if record_size=$(stat -c '%s' "$record" 2>/dev/null); then :; else
        metadata_status=$?
        printf -v pending_diagnostic '%sCannot run repository checks: image-id metadata at %q could not be read (status %s).\n' "$pending_diagnostic" "$record" "$metadata_status"
        exit 2
    fi
    if [[ "$record_size" != 72 ]] || ! LC_ALL=C grep -aExq 'sha256:[0-9a-f]{64}' "$record" 2>/dev/null; then
        printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot run repository checks: image-id must contain exactly one full immutable image ID and a newline; prepare checks/image/build explicitly.'
        exit 2
    fi
    if IFS= read -r checker_image_id 2>/dev/null < "$record"; then :; else
        metadata_status=$?
        printf -v pending_diagnostic '%sCannot run repository checks: image-id at %q could not be read (status %s).\n' "$pending_diagnostic" "$record" "$metadata_status"
        exit "$metadata_status"
    fi
    # Compare original bytes before using a shell value; Bash discards NUL bytes.
    if ! cmp -s -- "$record" <(printf '%s\n' "$checker_image_id" 2>/dev/null) 2>/dev/null; then
        printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot run repository checks: checker image record is not byte-exact.'
        exit 2
    fi
    if image_input_directory=$(mktemp -d "$repository_root/.work/checker/.inspect.XXXXXXXX" 2>/dev/null); then :; else
        metadata_status=$?
        image_input_directory=
        printf -v pending_diagnostic '%sCannot inspect checker image: private directory creation under %q failed (status %s); no private cause receipt was established.\n' \
            "$pending_diagnostic" "$repository_root/.work/checker" "$metadata_status"
        exit "$metadata_status"
    fi
    # Native receipts stay as bytes. Capture files are private (0700 directory,
    # 0600 files), with a 4 KiB per-file limit and a bounded inspect deadline.
    if container_native 'checker image inspection' 10 discard bash -eu -c '
        directory=$1; shift
        umask 077
        ulimit -f 4
        : > "$directory/observed-id"
        : > "$directory/diagnostic"
        exec "$@" > "$directory/observed-id" 2> "$directory/diagnostic"
    ' bash "$image_input_directory" "${engine[@]}" image inspect --format '{{.Id}}' "$checker_image_id"; then inspect_status=0; else inspect_status=$?; fi
    pending_diagnostic+=$inference_diagnostic
    if ! "$inference_retirement_confirmed"; then exit "$inspect_status"; fi
    if [[ $inspect_status != 0 ]]; then
        printf -v pending_diagnostic '%sCannot run repository checks: checker image inspection failed (status %s); prepare checks/image/build explicitly if the recorded image is unavailable.\n' "$pending_diagnostic" "$inspect_status"
    elif ! cmp -s -- "$image_input_directory/observed-id" <(printf '%s\n' "$checker_image_id" 2>/dev/null) 2>/dev/null; then
        inspect_status=2
        printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot run repository checks: observed checker image identity differs from its byte-exact immutable record.'
    else
        return 0
    fi
    # The failed inspection is retired. Keep its private cause for the operator,
    # as failed client preparation does; it is no longer runtime cleanup input.
    printf -v pending_diagnostic '%sChecker image inspection receipts retained at %q.\n' "$pending_diagnostic" "$image_input_directory"
    image_input_directory=
    exit "$inspect_status"
}

cleanup_launcher() {
    local status=$? removal_status=0 cancelled=0
    local native_cancellation=$inference_cancellation_status
    trap - EXIT
    trap 'cancelled=129' HUP
    trap 'cancelled=130' INT
    trap 'cancelled=143' TERM
    # A joined engine client alone cannot release a directory still writable by
    # a capture container. Both existing lifetime owners must confirm retirement.
    if "$inference_retirement_confirmed" && "$wheel_capture_retirement_confirmed"; then
        if [[ -n "$image_input_directory" ]]; then
            if rm -rf -- "$image_input_directory" 2>/dev/null; then image_input_directory=; else
                removal_status=$?
                printf -v pending_diagnostic '%sCannot confirm checker image receipt removal; attempted path %q (status %s).\n' "$pending_diagnostic" "$image_input_directory" "$removal_status"
                if [[ $status == 0 ]]; then status=$removal_status; fi
            fi
        fi
        if [[ -n "$check_input_directory" && ! "$print_command" == true ]]; then
            if rm -rf -- "$check_input_directory/git" 2>/dev/null; then :; else
                removal_status=$?
                printf -v pending_diagnostic '%sCannot confirm checker Git scaffold removal; attempted path %q (status %s).\n' "$pending_diagnostic" "$check_input_directory/git" "$removal_status"
                if [[ $status == 0 ]]; then status=$removal_status; fi
            fi
            if { rm -f -- "$check_input_directory/root-entries" && rmdir -- "$check_input_directory"; } 2>/dev/null; then :; else
                removal_status=$?
                printf -v pending_diagnostic '%sCannot confirm checker source inventory removal; attempted path %q (status %s).\n' "$pending_diagnostic" "$check_input_directory" "$removal_status"
                if [[ $status == 0 ]]; then status=$removal_status; fi
            fi
        fi
        if [[ -n "$probe_directory" && ! "$print_command" == true ]]; then
            if { rm -f -- "$probe_directory/ownership" && rmdir -- "$probe_directory"; } 2>/dev/null; then :; else
                removal_status=$?
                printf -v pending_diagnostic '%sCannot confirm probe output removal; attempted path %q (status %s).\n' "$pending_diagnostic" "$probe_directory" "$removal_status"
                if [[ $status == 0 ]]; then status=$removal_status; fi
            fi
        fi
        if docker_client_cleanup 2>/dev/null; then :; else
            removal_status=$?
            printf -v pending_diagnostic '%sCannot confirm Docker client configuration removal; attempted path %q (status %s).\n' "$pending_diagnostic" "$docker_client_directory" "$removal_status"
            if [[ $status == 0 ]]; then status=$removal_status; fi
        fi
    fi
    trap 'caller_cancellation=129; exit 129' HUP
    trap 'caller_cancellation=130; exit 130' INT
    trap 'caller_cancellation=143; exit 143' TERM
    if [[ $cancelled != 0 ]]; then exit "$cancelled"; fi
    if [[ $caller_cancellation != 0 ]]; then exit "$caller_cancellation"; fi
    if [[ $native_cancellation != 0 ]]; then exit "$native_cancellation"; fi
    if "$print_command" || [[ $network == host ]]; then
        # Preview and pre-dispatch download validation own no runtime client.
        printf '%s' "$pending_diagnostic" >&2
    elif ! "$inference_retirement_confirmed" || ! "$wheel_capture_retirement_confirmed"; then
        # Unknown retirement forbids another owner. Direct reporting keeps its
        # separate blocked-stderr limit; all owned resources remain untouched.
        printf '%s' "$pending_diagnostic" >&2
        printf 'Cannot confirm contained client or wheel-container retirement; owned state retained at %q, %q, %q and %q.\n' "$docker_client_directory" "$image_input_directory" "$check_input_directory" "$probe_directory" >&2
    elif [[ -n "$pending_diagnostic" ]]; then
        # Consume the payload before the reporter resets its result. It owns
        # its new fact: exit immediately, with no cleanup, retry or raw fallback.
        if container_native 'contained cleanup reporting' untimed stdio bash -eu -c \
            'printf "%s" "$1" >&2' bash "$pending_diagnostic"; then :; else
            local report_status=$?
            if [[ $inference_cancellation_status != 0 ]]; then status=$inference_cancellation_status
            elif [[ $status == 0 ]]; then status=$report_status; fi
        fi
    fi
    if [[ $status == 0 && $print_command == false && -n $next_actions ]]; then
        if report_next_actions "$next_actions"; then :; else status=$?; fi
    fi
    exit "$status"
}

trap cleanup_launcher EXIT
trap 'caller_cancellation=129; exit 129' HUP
trap 'caller_cancellation=130; exit 130' INT
trap 'caller_cancellation=143; exit 143' TERM

case "$1" in
    geometry/image/build|test/integration/rdkit-runtime|provider/image/build|delivery/image/build|test/contract/provider-core|test/integration/provider-core*|test/unit/provider-core*|test/integration/provider/proxy)
        source "$repository_root/tools/wheelhouse_path.sh"
        select_wheelhouse
        require_wheelhouse_pool
        ;;
esac

case "$1" in
    provider/image/build|delivery/image/build)
        build_kind=${1%%/*}
        if [[ "$caller_uid" == 0 || "$caller_gid" == 0 || "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
            printf -v pending_diagnostic '%s\n' 'Provider image builds require a nonzero UID/GID and a repository path without commas or line breaks.'
            exit 2
        fi
        for directory in tools requirements containers "containers/$build_kind" packages packages/provider-core; do
            require_source_directory "$directory"
        done
        if [[ $build_kind == provider ]]; then
            require_source_directory nmrnet_provider
            require_source_directory nmrnet_inference
        fi
        prepare_directory "$repository_root/.work"
        prepare_directory "$repository_root/.work/$build_kind"
        if [[ -d "$repository_root/.work/$build_kind" && $(stat -c '%u' -- "$repository_root/.work/$build_kind") != "$caller_uid" ]]; then
            printf -v pending_diagnostic 'Cannot capture %s build inputs: .work/%s must belong to the caller.\n' "$build_kind" "$build_kind"
            exit 2
        fi
        if "$print_command"; then docker_client_prepare print
        elif docker_client_prepare run; then :
        else
            preparation_status=$?; pending_diagnostic+=$docker_client_diagnostic; exit "$preparation_status"
        fi
        source "$repository_root/tools/${build_kind}_image.sh"
        "build_${build_kind}_image"
        exit
        ;;
    geometry/image/build)
        if [[ "$caller_uid" == 0 || "$caller_gid" == 0 || "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
            printf -v pending_diagnostic '%s\n' 'RDKit image builds require a nonzero UID/GID and a repository path without commas or line breaks.'
            exit 2
        fi
        for directory in tools requirements containers containers/geometry; do
            require_source_directory "$directory"
        done
        prepare_directory "$repository_root/.work"
        prepare_directory "$repository_root/.work/preparation"
        if [[ -d "$repository_root/.work/preparation" && $(stat -c '%u' -- "$repository_root/.work/preparation") != "$caller_uid" ]]; then
            printf -v pending_diagnostic '%s\n' 'Cannot capture RDKit build inputs: .work/preparation must belong to the caller.'
            exit 2
        fi
        if "$print_command"; then docker_client_prepare print
        elif docker_client_prepare run; then :
        else
            preparation_status=$?; pending_diagnostic+=$docker_client_diagnostic; exit "$preparation_status"
        fi
        source "$repository_root/tools/geometry_image.sh"
        build_geometry_image
        exit
        ;;
    download-base-image)
        # Explicit bootstrap exception: the daemon pulls the same pinned base
        # used below; WLAN_INTERFACE still governs package/artifact acquisition.
        # https://docs.docker.com/reference/cli/docker/image/pull/#pull-an-image-by-digest-immutable-identifier
        # https://docs.docker.com/engine/daemon/proxy/
        if "$print_command"; then
            docker_client_prepare print
            printf '%q ' "${engine[@]}" image pull "$image"; printf '\n'
            exit
        fi
        if docker_client_prepare run; then :; else
            preparation_status=$?
            pending_diagnostic+=$docker_client_diagnostic
            exit "$preparation_status"
        fi
        if container_native 'Python base image download' untimed stdio "${engine[@]}" image pull "$image"; then download_status=0; else download_status=$?; fi
        pending_diagnostic+=$inference_diagnostic
        if [[ "$download_status" != 0 ]]; then
            printf -v pending_diagnostic '%sPython base image download did not finish successfully (status %s). Docker may retain layers or finish the download after its client stops. Retry make packages/base-images/python/download; no image removal was attempted.\n' \
                "$pending_diagnostic" "$download_status"
        else
            source "$repository_root/tools/operator_guidance.sh"
            next_actions=$(next_make_command 'To build the inference image, acquire its dependencies next if needed:' packages/wheelhouse)
        fi
        exit "$download_status"
        ;;
    pull)
        printf '%s\n' 'Base images must be preloaded: Docker daemon pulls cannot be bound by this launcher to WLAN_INTERFACE.' >&2
        printf 'To download this pinned base using Docker default routing, run make packages/base-images/python/download: %s\n' "$image" >&2
        exit 2
        ;;
    test/unit/provider/deployment|test/unit/provider/api-stage|test/unit/provider/api-client|test/unit/provider/result|\
    test/unit/provider/proxy|test/integration/provider/proxy|test/unit/provider/image|test/integration/provider-runtime|test/integration/native-smiles-worker|test/unit/smiles-worker|test/unit/smiles-protocol|test/integration/smiles-generator|test/integration/native-geometry-worker|test/integration/sdf-reader|test/integration/mol-reader|test/integration/rdkit-worker|test/integration/rdkit-runtime|test/unit/packages|test/unit/packages/wheelhouse|test/contract/provider-core|test/integration/provider-core|test/integration/provider-core/prepared-identity|test/integration/provider-core/uploads|test/integration/provider-core/smiles|test/integration/provider-core/pasted|test/integration/provider-core/output|test/integration/provider-core/interpreter|test/integration/provider-core/lifecycle|test/integration/provider-core/runtime|test/integration/provider-core/runner|test/integration/provider-core/native-factory|test/integration/provider-core/admission|test/integration/provider-core/service|test/integration/provider-core/recovery|test/integration/provider-core/interpreter-ambiguity|test/integration/provider-core/process|test/integration/provider-core/config|test/integration/provider-core/compose|test/integration/provider-core/chat|test/integration/provider-core/delivery|test/integration/provider-core/hello|test/integration/provider-core/transport|test/integration/provider-core/api|test/unit/provider-core|test/unit/provider-core/verbosity|test/unit/provider-core/credential|test/unit/provider-core/hello|test/unit/provider-core/transport|test/unit/provider-core/api|test/unit/provider-core/admission|test/unit/provider-core/uploads|test/unit/provider-core/diagnostics|test/unit/provider-core/delivery|test/unit/provider-core/checkpoints|test/unit/provider-core/recovery|test/unit/provider-core/normative|test/unit/provider-core/adversarial|test/unit/provider-core/runner|test/unit/provider-core/prepared-identity|test|test/unit/provider/worker|test/unit/provider|test/unit/provider/pdb|test/unit/provider/mol2|test/unit/provider-geometry-worker|test/unit/provider/worker-startup|test/unit/provider/host|test/unit/bond-distances|test/unit/prediction-input|test/unit/baseline-prepare|test/unit/baseline-run|test/unit/baseline-model|test/unit/baseline-launcher|test/repository|test/repository/native-images|test/repository/wheelhouse|test/repository/documentation|test/repository/operator-inputs|test/repository/provider-core|probe|download|check|inspect|extract)
        if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
            printf '%s\n' 'Contained lanes require a nonzero host UID and GID.' >&2
            exit 2
        fi
        if [[ "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
            printf '%s\n' 'Repository path cannot contain commas or line breaks in Docker bind mounts.' >&2
            exit 2
        fi
        if [[ "$1" == download ]]; then
            source "$repository_root/tools/acquisition_interface.sh"
            select_acquisition_interface
        fi
        if [[ "$1" == download || "$1" == check || "$1" == inspect || "$1" == extract ]]; then
            # Make owns public input origin and hands over only its literal
            # value alias. Ambient ARCHIVE must not select an archive action.
            if [[ ! -v NMRNET_ARCHIVE_INPUT ]] || [[ ! "$NMRNET_ARCHIVE_INPUT" =~ ^[A-Za-z0-9_.-]+\.zip$ ]]; then
                printf '%s\n' 'Select an archive on the make command line, for example make weights/check ARCHIVE=weight.zip.' >&2
                exit 2
            fi
            archive="$NMRNET_ARCHIVE_INPUT"
            source "$repository_root/tools/operator_guidance.sh"
            case "$1" in
                download|check) next_actions=$(next_make_command 'Next, extract the declared members if not already extracted:' weights/extract "ARCHIVE=$archive") ;;
                inspect) next_actions=$(next_make_command 'To extract the declared members, if needed:' weights/extract "ARCHIVE=$archive") ;;
                extract)
                    case "$archive" in
                        weight.zip|checkpoints.zip)
                            if [[ $archive == weight.zip ]]; then other_archive=checkpoints.zip; else other_archive=weight.zip; fi
                            next_actions=$(next_make_command 'If the other model archive is not yet prepared, acquire it next:' weights/download "ARCHIVE=$other_archive"; \
                                printf '%s\n' 'Once weight.zip and checkpoints.zip are both extracted, finish the images and service configuration:' \
                                '  https://numpde.github.io/nmrnet-repro/provider.html#provider-startup-configuration') ;;
                        data.zip)
                            next_actions=$(next_make_command 'For baseline reproduction, after weight.zip is also extracted:' baseline/prepare) ;;
                    esac ;;
            esac
            if [[ $1 == download ]]; then
                next_actions=$(printf 'Verified archive available at %s/.work/downloads/%s\n' "$repository_root" "$archive"; printf '%s\n' "$next_actions")
            elif [[ $1 == extract ]]; then
                next_actions=$(printf 'Extracted members to %s/.work/prepared/%s/\n' "$repository_root" "${archive%.zip}"; printf '%s\n' "$next_actions")
            fi
            if [[ "$1" == download ]]; then
                network=host
                prepare_directory "$repository_root/.work"
                prepare_directory "$repository_root/.work/downloads"
            elif [[ -L "$repository_root/.work" || -L "$repository_root/.work/downloads" || ! -d "$repository_root/.work/downloads" ]]; then
                printf '%s\n' 'Cannot read archives: .work/downloads must be an existing directory without symlinks.' >&2
                exit 2
            fi
            if [[ "$1" == extract ]]; then
                selection="$repository_root/artifacts/${archive%.zip}.selection.json"
                if [[ ! -f "$selection" || -L "$selection" ]]; then
                    printf 'Cannot extract %s: expected a regular selection declaration at %s.\n' "$archive" "$selection" >&2
                    exit 2
                fi
                prepare_directory "$repository_root/.work/prepared"
            fi
        fi
        if [[ "$network" == host ]]; then
            command=(bash "$repository_root/tools/docker.sh")
            if "$print_command"; then command+=(--print); fi
            command+=(--success-note "$next_actions" run)
        else
            if "$print_command"; then docker_client_prepare print
            elif docker_client_prepare run; then :
            else
                preparation_status=$?
                pending_diagnostic+=$docker_client_diagnostic
                exit "$preparation_status"
            fi
            command=("${engine[@]}" run --rm --network none)
        fi
        command+=(--pull never --read-only --env NMRNET_IN_CONTAINER=1
            --user "$caller_uid:$caller_gid" --cap-drop ALL
            --security-opt no-new-privileges:true
            --cpus 2 --memory 512m --memory-swap 512m --pids-limit 64
            --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m
            --workdir /workspace)
        if [[ "$1" == test/unit/provider/result || "$1" == test || "$1" == test/unit/provider || "$1" == test/unit/provider-geometry-worker || "$1" == test/unit/provider/worker-startup || "$1" == test/unit/provider/host || "$1" == test/unit/bond-distances || "$1" == test/unit/prediction-input || "$1" == test/unit/baseline-prepare || "$1" == test/unit/baseline-run || "$1" == test/unit/baseline-model || "$1" == test/unit/baseline-launcher || "$1" == test/repository || "$1" == test/repository/documentation || "$1" == test/repository/operator-inputs || "$1" == test/repository/provider-core || "$1" == test/repository/native-images || "$1" == test/repository/wheelhouse ]]; then
            # Native cancellation probes retire whole process groups. An init
            # process reaps orphaned descendants; Python as PID 1 would retain
            # zombies until the fixed 64-process budget was exhausted.
            command+=(--init)
        fi
        case "$1" in
            test/unit/provider/api-stage)
                for directory in tools tests/integration/provider_runtime tests/integration/provider_core packages/provider-core/tests .work .work/provider; do require_source_directory "$directory"; done
                source "$repository_root/tools/provider_image.sh"
                require_provider_image
                command+=(--init --entrypoint /opt/provider/environment/bin/python --env PYTHONPATH=/fixture
                    --mount "type=bind,src=$repository_root/config/provider.toml.example,dst=/fixture/provider.toml.example,readonly"
                    --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/stage_api_config.py,dst=/fixture/stage_api_config.py,readonly"
                    --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/normative/test_stage_api_config.py,dst=/fixture/test_stage_api_config_normative.py,readonly"
                    --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/adversarial/test_stage_api_config.py,dst=/fixture/test_stage_api_config_adversarial.py,readonly"
                    --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/staging_test_ports.py,dst=/fixture/staging_test_ports.py,readonly"
                    --mount "type=bind,src=$repository_root/tests/integration/provider_core/config_ports.py,dst=/fixture/config_ports.py,readonly"
                    --mount "type=bind,src=$repository_root/packages/provider-core/tests/__init__.py,dst=/fixture/tests/__init__.py,readonly"
                    --mount "type=bind,src=$repository_root/packages/provider-core/tests/fakes/__init__.py,dst=/fixture/tests/fakes/__init__.py,readonly"
                    --mount "type=bind,src=$repository_root/packages/provider-core/tests/fakes/credential.py,dst=/fixture/tests/fakes/credential.py,readonly"
                    "$provider_image_id" -B -m unittest -v test_stage_api_config_normative test_stage_api_config_adversarial)
                ;;
            test/unit/provider/api-client)
                for directory in tools tests/integration/provider_runtime; do require_source_directory "$directory"; done
                api_test_image=${NMRNET_API_CLIENT_IMAGE_INPUT-}
                [[ $api_test_image =~ ^sha256:[0-9a-f]{64}$ ]] || {
                    pending_diagnostic+='API client tests require explicit API_CLIENT_IMAGE as an immutable local image ID. '
                    exit 2
                }
                command+=(--init --entrypoint /usr/local/bin/python --env PYTHONPATH=/fixture
                    --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/api_prediction.py,dst=/fixture/api_prediction.py,readonly"
                    --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/normative/test_api_prediction.py,dst=/fixture/test_api_prediction_normative.py,readonly"
                    --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/adversarial/test_api_prediction.py,dst=/fixture/test_api_prediction_adversarial.py,readonly"
                    --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/prediction_test_ports.py,dst=/fixture/prediction_test_ports.py,readonly"
                    --mount "type=bind,src=$repository_root/tests/fixtures/preparation/normative/butane_conformers.sdf,dst=/fixture/butane_conformers.sdf,readonly"
                    --mount "type=bind,src=$repository_root/tests/fixtures/preparation/smiles.json,dst=/fixture/smiles.json,readonly"
                    --mount "type=bind,src=$repository_root/tests/fixtures/preparation/expected.json,dst=/fixture/expected.json,readonly"
                    --mount "type=bind,src=$repository_root/tests/fixtures/preparation/normative/atom_order.xyz,dst=/fixture/atom_order.xyz,readonly"
                    --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/fixture/tools/container_guard.py,readonly"
                    --mount "type=bind,src=$repository_root/tools/bound_http_proxy.py,dst=/fixture/tools/bound_http_proxy.py,readonly"
                    "$api_test_image" -B -m unittest -v test_api_prediction_normative test_api_prediction_adversarial)
                ;;
            test/unit/provider/proxy)
                for directory in tools tests repository_checks; do require_source_directory "$directory"; done
                checker_image
                command+=(--init --env PROVIDER_CORE_IN_CONTAINER=1
                    --mount "type=bind,src=$repository_root/packages/provider-core/src/provider_core,dst=/workspace/provider_core,readonly"
                    --mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/tests,dst=/workspace/tests,readonly"
                    --mount "type=bind,src=$repository_root/repository_checks,dst=/workspace/repository_checks,readonly"
                    "$checker_image_id" python3 -B -m unittest -v
                    tests.unit.normative.test_provider_proxy tests.unit.adversarial.test_provider_proxy
                    tests.test_bound_http_proxy repository_checks.test_proxy_posture)
                ;;
            test/unit/provider/image)
                for directory in tools tests requirements; do require_source_directory "$directory"; done
                if [[ ! -f "$repository_root/Makefile" || -L "$repository_root/Makefile" ]]; then
                    printf -v pending_diagnostic '%s\n' 'Cannot check provider image commands: Makefile must be a regular file without a symlink.'
                    exit 2
                fi
                if [[ ! -f "$repository_root/requirements/provider-core-build.lock" || -L "$repository_root/requirements/provider-core-build.lock" ]]; then
                    printf -v pending_diagnostic '%s\n' 'Cannot check provider installation: the build lock must be a regular file without a symlink.'
                    exit 2
                fi
                checker_image
                command+=(--init
                    --mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/tests,dst=/workspace/tests,readonly"
                    --mount "type=bind,src=$repository_root/Makefile,dst=/workspace/Makefile,readonly"
                    --mount "type=bind,src=$repository_root/requirements/provider-core-build.lock,dst=/input/provider-core-build.lock,readonly"
                    "$checker_image_id" python3 -B -m unittest -v
                    tests.unit.normative.test_provider_image tests.unit.adversarial.test_provider_image tests.unit.normative.test_delivery_image)
                ;;
            test/integration/provider-runtime)
                for directory in tools tests/integration/provider_runtime .work .work/provider; do require_source_directory "$directory"; done
                source "$repository_root/tools/provider_image.sh"
                require_provider_image
                command+=(--init --entrypoint /opt/provider/environment/bin/python
                    --mount "type=bind,src=$repository_root/tests/integration/provider_runtime,dst=/provider-tests,readonly"
                    --workdir /tmp "$provider_image_id" -I -B -m unittest discover -s /provider-tests -p test_runtime.py -v)
                ;;
            test/integration/rdkit-worker|test/integration/mol-reader|test/integration/sdf-reader|test/integration/smiles-generator|test/integration/native-smiles-worker|test/integration/native-geometry-worker)
                for directory in tools tests tests/integration tests/integration/rdkit_runtime requirements; do
                    require_source_directory "$directory"
                done
                if [[ ! -f "$repository_root/requirements/preparation.lock" || -L "$repository_root/requirements/preparation.lock" ]]; then
                    printf -v pending_diagnostic '%s\n' 'Cannot check the RDKit image: requirements/preparation.lock must be a regular file without a symlink.'
                    exit 2
                fi
                source "$repository_root/tools/geometry_image.sh"
                require_geometry_image
                command+=(--init
                    --mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/tests/integration/rdkit_runtime,dst=/native-tests,readonly"
                    --mount "type=bind,src=$repository_root/requirements/preparation.lock,dst=/input/preparation.lock,readonly")
                if [[ "$1" != test/integration/rdkit-worker ]]; then
                    for directory in nmrnet_provider nmrnet_inference tests/fixtures tests/fixtures/preparation; do
                        require_source_directory "$directory"
                    done
                    case "$1" in
                        test/integration/mol-reader) native_suite=mol ;;
                        test/integration/sdf-reader) native_suite=sdf ;;
                        test/integration/smiles-generator) native_suite=smiles ;;
                        test/integration/native-smiles-worker) native_suite=smiles_worker ;;
                        test/integration/native-geometry-worker) native_suite=geometry_worker ;;
                    esac
                    command+=(--mount "type=bind,src=$repository_root/nmrnet_provider,dst=/workspace/nmrnet_provider,readonly"
                        --mount "type=bind,src=$repository_root/nmrnet_inference,dst=/workspace/nmrnet_inference,readonly"
                        --mount "type=bind,src=$repository_root/tests/fixtures/preparation,dst=/geometry-fixtures,readonly"
                        "$geometry_image_id" /opt/rdkit/environment/bin/python -B -m tools.rdkit_runtime_tests "$native_suite")
                else
                    command+=("$geometry_image_id" /opt/rdkit/environment/bin/python -B -m tools.rdkit_runtime_tests runtime)
                fi
                ;;
            test/integration/rdkit-runtime)
                for directory in tools tests tests/integration tests/integration/rdkit_runtime requirements; do
                    require_source_directory "$directory"
                done
                if [[ ! -f "$repository_root/requirements/preparation.lock" || -L "$repository_root/requirements/preparation.lock" ]]; then
                    printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot test RDKit dependencies: requirements/preparation.lock must be a regular file without a symlink.'
                    exit 2
                fi
                command+=(--init --tmpfs /runtime:rw,nosuid,nodev,exec,size=512m
                    --mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/tests/integration/rdkit_runtime,dst=/native-tests,readonly"
                    --mount "type=bind,src=$repository_root/requirements/preparation.lock,dst=/input/preparation.lock,readonly"
                    --mount "type=bind,src=$wheelhouse_directory,dst=/runtime-deps,readonly"
                    "$image" python3 -B -m tools.rdkit_runtime_tests temporary)
                ;;
            test/unit/packages|test/unit/packages/wheelhouse)
                require_source_directory tools
                require_source_directory tests
                command+=(--init
                    --mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/tests,dst=/workspace/tests,readonly"
                    "$image" python3 -B -m unittest -v)
                if [[ "$1" == test/unit/packages/wheelhouse ]]; then
                    command+=(tests.test_wheelhouse tests.test_inference_image
                        tests.unit.normative.test_wheelhouse_path tests.unit.adversarial.test_wheelhouse_path
                        tests.unit.normative.test_wheel_pool)
                else
                    command+=(tests.test_package_lock tests.test_wheelhouse tests.test_guarded_tooling)
                fi
                ;;
            test/integration/provider/proxy|test/contract/provider-core|test/integration/provider-core|test/integration/provider-core/prepared-identity|test/integration/provider-core/uploads|test/integration/provider-core/smiles|test/integration/provider-core/pasted|test/integration/provider-core/output|test/integration/provider-core/interpreter|test/integration/provider-core/lifecycle|test/integration/provider-core/runtime|test/integration/provider-core/runner|test/integration/provider-core/native-factory|test/integration/provider-core/admission|test/integration/provider-core/service|test/integration/provider-core/recovery|test/integration/provider-core/interpreter-ambiguity|test/integration/provider-core/process|test/integration/provider-core/config|test/integration/provider-core/compose|test/integration/provider-core/chat|test/integration/provider-core/delivery|test/integration/provider-core/hello|test/integration/provider-core/transport|test/integration/provider-core/api|test/unit/provider-core|test/unit/provider-core/verbosity|test/unit/provider-core/credential|test/unit/provider-core/hello|test/unit/provider-core/transport|test/unit/provider-core/api|test/unit/provider-core/admission|test/unit/provider-core/uploads|test/unit/provider-core/diagnostics|test/unit/provider-core/delivery|test/unit/provider-core/checkpoints|test/unit/provider-core/recovery|test/unit/provider-core/normative|test/unit/provider-core/adversarial|test/unit/provider-core/runner|test/unit/provider-core/prepared-identity)
                require_source_directory tools
                require_source_directory packages
                require_source_directory packages/provider-core
                require_source_directory requirements
                for core_file in "$repository_root/requirements/provider-core-build.lock" "$repository_root/requirements/provider-core.lock" "$repository_root/tools/container_guard.py"; do
                    if [[ ! -f "$core_file" || -L "$core_file" ]]; then
                        printf -v pending_diagnostic '%sCannot test provider-core: expected a regular build input at %s.\n' "$pending_diagnostic" "$core_file"
                        exit 2
                    fi
                done
                core_suite=unit
                if [[ "$1" == test/contract/provider-core ]]; then core_suite=contract; fi
                if [[ "$1" == test/integration/provider/proxy || "$1" == test/integration/provider-core || "$1" == test/integration/provider-core/prepared-identity || "$1" == test/integration/provider-core/uploads || "$1" == test/integration/provider-core/smiles || "$1" == test/integration/provider-core/pasted || "$1" == test/integration/provider-core/lifecycle || "$1" == test/integration/provider-core/runtime || "$1" == test/integration/provider-core/runner || "$1" == test/integration/provider-core/native-factory || "$1" == test/integration/provider-core/admission || "$1" == test/integration/provider-core/service || "$1" == test/integration/provider-core/recovery || "$1" == test/integration/provider-core/interpreter-ambiguity || "$1" == test/integration/provider-core/process || "$1" == test/integration/provider-core/config || "$1" == test/integration/provider-core/compose || "$1" == test/integration/provider-core/output || "$1" == test/integration/provider-core/interpreter ]]; then
                    core_suite=integration
                    require_source_directory nmrnet_provider
                    require_source_directory nmrnet_inference
                    require_source_directory tests
                    require_source_directory tests/integration
                    require_source_directory tests/integration/provider_core
                    if [[ ! -f "$repository_root/tools/artifacts.py" || -L "$repository_root/tools/artifacts.py" ]]; then
                        printf -v pending_diagnostic '%sCannot test provider startup: tools/artifacts.py must be a regular source file without a symlink.\n' "$pending_diagnostic"
                        exit 2
                    fi
                    command+=(--mount "type=bind,src=$repository_root/nmrnet_provider,dst=/consumer/nmrnet_provider,readonly"
                        --mount "type=bind,src=$repository_root/tests/integration/provider_core,dst=/consumer-tests,readonly"
                        --mount "type=bind,src=$repository_root/nmrnet_inference,dst=/consumer/nmrnet_inference,readonly"
                        --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/consumer/tools/container_guard.py,readonly"
                        --mount "type=bind,src=$repository_root/tools/artifacts.py,dst=/consumer/tools/artifacts.py,readonly")
                    integration_sources=(bound_http_proxy.py provider_proxy.py)
                    if [[ "$1" == test/integration/provider-core ]]; then
                        integration_sources+=(container_resources.py provider_deployment.py)
                    elif [[ "$1" == test/integration/provider-core/runner ]]; then
                        integration_sources+=(container_resources.py)
                    elif [[ "$1" == test/integration/provider-core/compose ]]; then
                        integration_sources+=(provider_deployment.py)
                    fi
                    for integration_source in "${integration_sources[@]}"; do
                        if [[ ! -f "$repository_root/tools/$integration_source" || -L "$repository_root/tools/$integration_source" ]]; then
                            printf -v pending_diagnostic '%sCannot test provider integration: expected a regular authored tool source at %s.\n' "$pending_diagnostic" "tools/$integration_source"
                            exit 2
                        fi
                        command+=(--mount "type=bind,src=$repository_root/tools/$integration_source,dst=/consumer/tools/$integration_source,readonly")
                    done
                fi
                if [[ "$1" == test/integration/provider-core || "$1" == test/integration/provider-core/uploads ]]; then
                    require_source_directory tests/fixtures
                    require_source_directory tests/fixtures/preparation
                    require_source_directory tests/fixtures/preparation/normative
                    for fixture in normative/mixed_records.xyz expected.json; do
                        if [[ ! -f "$repository_root/tests/fixtures/preparation/$fixture" || -L "$repository_root/tests/fixtures/preparation/$fixture" ]]; then
                            printf -v pending_diagnostic '%sCannot test Upload preparation: expected a regular fixture at %s.\n' "$pending_diagnostic" "$fixture"
                            exit 2
                        fi
                    done
                    command+=(--mount "type=bind,src=$repository_root/tests/fixtures/preparation/normative/mixed_records.xyz,dst=/fixtures/mixed_records.xyz,readonly"
                        --mount "type=bind,src=$repository_root/tests/fixtures/preparation/expected.json,dst=/fixtures/expected.json,readonly")
                fi
                if [[ "$1" == test/integration/provider-core || "$1" == test/integration/provider-core/lifecycle ]]; then
                    require_source_directory tests/fixtures/preparation
                    command+=(--mount "type=bind,src=$repository_root/tests/fixtures/preparation,dst=/fixtures/preparation,readonly")
                fi
                if [[ "$1" == test/integration/provider-core || "$1" == test/integration/provider-core/interpreter-ambiguity ]]; then
                    for probe_input in tests/integration/provider_runtime/interpreter_ambiguity.py tests/integration/provider_runtime/stage_api_config.py tests/integration/provider_core/config_ports.py tests/fixtures/preparation/interpreter_ambiguity.json tests/fixtures/preparation/interpreter_formats.json; do
                        if [[ ! -f "$repository_root/$probe_input" || -L "$repository_root/$probe_input" ]]; then
                            printf -v pending_diagnostic '%sCannot test the interpreter probe: expected a regular authored input.\n' "$pending_diagnostic"
                            exit 2
                        fi
                        probe_target=/consumer/${probe_input##*/}
                        if [[ $probe_input == *.json ]]; then probe_target=/fixture/${probe_input##*/}; fi
                        command+=(--mount "type=bind,src=$repository_root/$probe_input,dst=$probe_target,readonly")
                    done
                fi
                if [[ "$1" == */normative ]]; then core_suite=unit/normative; fi
                if [[ "$1" == */adversarial ]]; then core_suite=unit/adversarial; fi
                if [[ "$1" == test/integration/provider-core/chat ]]; then core_suite=integration/chat; fi
                if [[ "$1" == test/integration/provider-core/delivery ]]; then core_suite=integration/delivery; fi
                if [[ "$1" == test/integration/provider-core/hello ]]; then core_suite=integration/hello; fi
                if [[ "$1" == test/integration/provider-core/transport ]]; then core_suite=integration/transport; fi
                if [[ "$1" == test/integration/provider-core/api ]]; then core_suite=integration/api; fi
                if [[ "$1" == test/integration/provider-core/compose || "$1" == test/integration/provider-core ]]; then
                    require_source_directory tests/fixtures/scientific_inputs
                    command+=(--mount "type=bind,src=$repository_root/tests/fixtures/scientific_inputs,dst=/fixture/scientific_inputs,readonly"
                        --mount "type=bind,src=$repository_root/tests/integration/provider_runtime/compose_peer.py,dst=/consumer/compose_peer.py,readonly"
                        --mount "type=bind,src=$repository_root/artifacts/reference-cpu-float32.json,dst=/fixture/reference-cpu-float32.json,readonly"
                        --mount "type=bind,src=$repository_root/artifacts/prediction-cpu-float32.json,dst=/fixture/prediction-cpu-float32.json,readonly")
                fi
                command+=(--init --env PROVIDER_CORE_IN_CONTAINER=1
                    --tmpfs /runtime:rw,nosuid,nodev,exec,size=128m
                    --mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/packages/provider-core,dst=/package,readonly"
                    --mount "type=bind,src=$repository_root/requirements/provider-core-build.lock,dst=/core-build.lock,readonly"
                    --mount "type=bind,src=$repository_root/requirements/provider-core.lock,dst=/core-runtime.lock,readonly"
                    --mount "type=bind,src=$wheelhouse_directory,dst=/runtime-deps,readonly"
                    --mount "type=bind,src=$wheelhouse_directory,dst=/build-deps,readonly"
                    "$image" python3 -B -m tools.provider_core_tests "$core_suite")
                if [[ "$1" == test/integration/provider-core/uploads ]]; then
                    command+=(--pattern test_nmrnet_upload_session.py test_upload_preparation.py test_nmrnet_upload_preparation.py test_upload_acquisition.py test_secs_sources.py)
                fi
                if [[ "$1" == test/integration/provider-core/smiles ]]; then
                    command+=(--pattern 'test_nmrnet_smiles_*.py')
                fi
                if [[ "$1" == test/integration/provider-core/pasted ]]; then
                    command+=(--pattern test_nmrnet_pasted.py)
                fi
                if [[ "$1" == test/integration/provider-core/interpreter ]]; then
                    command+=(--pattern test_nmrnet_interpreter.py test_nmrnet_interpreter_preparation.py)
                fi
                if [[ "$1" == test/integration/provider-core/output ]]; then
                    command+=(--pattern test_nmrnet_input.py test_nmrnet_session.py test_nmrnet_prediction_recovery.py)
                fi
                if [[ "$1" == test/integration/provider-core/native-factory ]]; then
                    command+=(--pattern test_nmrnet_native_factory.py)
                fi
                if [[ "$1" == test/integration/provider-core/config ]]; then
                    command+=(--pattern test_nmrnet_config.py)
                fi
                if [[ "$1" == test/integration/provider-core/compose ]]; then
                    command+=(--pattern test_provider_compose.py test_provider_deployment.py test_compose_scientific_inputs.py)
                fi
                if [[ "$1" == test/integration/provider-core/runtime ]]; then
                    command+=(--pattern test_nmrnet_runtime.py)
                fi
                if [[ "$1" == test/integration/provider-core/admission ]]; then
                    command+=(--pattern test_nmrnet_service_admission.py)
                fi
                if [[ "$1" == test/integration/provider-core/service ]]; then
                    command+=(--pattern test_nmrnet_service.py test_nmrnet_service_recovery.py)
                fi
                if [[ "$1" == test/integration/provider-core/recovery ]]; then
                    command+=(--pattern test_nmrnet_recovery.py test_nmrnet_recovery_capture.py test_publication.py test_terminal_store.py)
                fi
                if [[ "$1" == test/integration/provider-core/interpreter-ambiguity ]]; then
                    command+=(--pattern test_nmrnet_interpreter_ambiguity.py)
                fi
                if [[ "$1" == test/integration/provider-core/process ]]; then
                    command+=(--pattern test_nmrnet_process.py test_nmrnet_reports.py)
                fi
                if [[ "$1" == test/integration/provider/proxy ]]; then
                    command+=(--pattern test_nmrnet_proxy.py)
                fi
                if [[ "$1" == test/integration/provider-core/lifecycle ]]; then
                    command+=(--pattern 'test_nmrnet_*_lifecycle.py')
                fi
                if [[ "$1" == test/unit/provider-core/runner ]]; then
                    command+=(--pattern test_runner.py test_process_status.py)
                fi
                if [[ "$1" == test/integration/provider-core/runner ]]; then
                    command+=(--pattern 'test_nmrnet_runner_*.py')
                fi
                if [[ "$1" == test/unit/provider-core/prepared-identity ]]; then
                    command+=(--pattern test_prepared_identity.py 'test_execution*.py'
                        test_attempt_transitions.py test_terminal_record.py test_recovery.py)
                fi
                if [[ "$1" == test/unit/provider-core/credential ]]; then
                    command+=(--pattern test_credential.py test_signing.py)
                fi
                if [[ "$1" == test/unit/provider-core/verbosity ]]; then
                    command+=(--pattern test_result_verbosity.py)
                fi
                if [[ "$1" == test/unit/provider-core/hello ]]; then
                    command+=(--pattern test_hello.py)
                fi
                if [[ "$1" == test/unit/provider-core/transport ]]; then
                    command+=(--pattern test_transport.py test_network.py)
                fi
                if [[ "$1" == test/unit/provider-core/api ]]; then
                    command+=(--pattern 'test_api*.py' test_attempt_start.py test_attempt_progress.py test_attempt_outcome.py test_upload_api.py test_publication.py)
                fi
                if [[ "$1" == test/unit/provider-core/admission ]]; then
                    command+=(--pattern test_admission_inventory.py test_attempt_store.py test_attempt_history.py)
                fi
                if [[ "$1" == test/unit/provider-core/uploads ]]; then
                    command+=(--pattern test_upload_acquisition.py test_upload_file_acquisition.py test_upload_snapshot.py)
                fi
                if [[ "$1" == test/unit/provider-core/diagnostics ]]; then
                    command+=(--pattern test_failure_projection.py)
                fi
                if [[ "$1" == test/unit/provider-core/delivery ]]; then
                    command+=(--pattern 'test_delivery*.py')
                fi
                if [[ "$1" == test/unit/provider-core/checkpoints ]]; then
                    command+=(--pattern 'test*checkpoint*.py')
                fi
                if [[ "$1" == test/unit/provider-core/recovery ]]; then
                    command+=(--pattern test_terminal_record.py test_publication.py test_recovery.py 'test_controller*.py' test_failure_projection.py)
                fi
                if [[ "$1" == test/integration/provider-core/prepared-identity ]]; then
                    command+=(--pattern test_nmrnet_session.py test_nmrnet_prediction_recovery.py test_attempt_store.py test_terminal_store.py
                        test_magnet_adaptation.py test_child_retirement.py test_execution.py)
                fi
                ;;
            test/unit/provider/deployment|test/unit/provider/result|\
            test|test/unit/smiles-worker|test/unit/smiles-protocol|test/unit/provider/worker|test/unit/provider|test/unit/provider/pdb|test/unit/provider/mol2|test/unit/provider-geometry-worker|test/unit/provider/worker-startup|test/unit/provider/host|test/unit/bond-distances|test/unit/prediction-input|test/unit/baseline-prepare|test/unit/baseline-run|test/unit/baseline-model|test/unit/baseline-launcher)
                require_source_directory tools
                require_source_directory tests
                require_source_directory nmrnet_inference
                require_source_directory nmrnet_provider
                require_source_directory packages/provider-core/src/provider_core
                # Source-unit tests see the real package initializer and its pure
                # vocabulary; installed-package/runtime proof uses separate lanes.
                unit_tests=(discover -s tests)
                if [[ "$1" == test/unit/provider/deployment ]]; then
                    unit_tests=(-v tests.test_provider_deployment_launcher
                        test_api_qualification_normative
                        tests.test_docker_client.DockerClientTests.test_actual_client_sees_empty_private_config_and_fixed_environment)
                    command+=(--mount "type=bind,src=$repository_root/tests/integration/provider_runtime/normative/test_api_qualification.py,dst=/workspace/test_api_qualification_normative.py,readonly"
                        --mount "type=bind,src=$repository_root/tools/provider_api_qualification.sh,dst=/fixture/provider_api_qualification.sh,readonly"
                        --mount "type=bind,src=$repository_root/tools/operator_guidance.sh,dst=/fixture/operator_guidance.sh,readonly")
                fi
                command+=(--mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/tests,dst=/workspace/tests,readonly"
                    --mount "type=bind,src=$repository_root/nmrnet_inference,dst=/workspace/nmrnet_inference,readonly"
                    --mount "type=bind,src=$repository_root/nmrnet_provider,dst=/workspace/nmrnet_provider,readonly"
                    --mount "type=bind,src=$repository_root/packages/provider-core/src/provider_core,dst=/workspace/provider_core,readonly"
                    --env PROVIDER_CORE_IN_CONTAINER=1
                    "$image" python3 -B -m unittest "${unit_tests[@]}")
                if [[ "$1" == test ]]; then
                    command+=(-v)
                elif [[ "$1" == test/unit/smiles-protocol ]]; then
                    command+=(-p test_smiles_protocol.py -v)
                elif [[ "$1" == test/unit/smiles-worker ]]; then
                    command+=(-p test_smiles_worker.py -v)
                elif [[ "$1" == test/unit/provider/worker ]]; then
                    command+=(-p test_provider_worker.py -v)
                elif [[ "$1" == test/unit/provider ]]; then
                    command+=(-p "test_provider_*.py" -v)
                elif [[ "$1" == test/unit/provider/pdb || "$1" == test/unit/provider/mol2 ]]; then
                    command+=(-p "test_provider_${1##*/}.py" -v)
                elif [[ "$1" == test/unit/provider/host ]]; then
                    command+=(-p test_provider_host.py -v)
                elif [[ "$1" == test/unit/provider/deployment ]]; then
                    : # Complete fixed deployment/client selection above.
                elif [[ "$1" == test/unit/provider/result ]]; then
                    command+=(-p 'test_provider_*result*.py' -v)
                elif [[ "$1" == test/unit/provider/worker-startup ]]; then
                    command+=(-p test_worker_main.py -v)
                elif [[ "$1" == test/unit/provider-geometry-worker ]]; then
                    command+=(-p 'test_provider_geometry*.py' -v)
                elif [[ "$1" == test/unit/bond-distances ]]; then
                    command+=(-p test_bond_distances.py -v)
                elif [[ "$1" == test/unit/prediction-input ]]; then
                    command+=(-p test_prediction_input.py -v)
                elif [[ "$1" == test/unit/baseline-prepare ]]; then
                    command+=(-p test_baseline_prepare.py -v)
                elif [[ "$1" == test/unit/baseline-launcher ]]; then
                    command+=(-p test_baseline_launcher.py -v)
                elif [[ "$1" == test/unit/baseline-model ]]; then
                    command+=(-p test_baseline_model.py -v)
                else
                    command+=(-p test_baseline_run.py -v)
                fi
                ;;
            test/repository|test/repository/native-images|test/repository/wheelhouse|test/repository/documentation|test/repository/operator-inputs|test/repository/provider-core)
                # Checkers read public source inputs, never workspace state or credentials.
                # A root-entry inventory makes omitted new source paths fail visibly.
                require_source_directory tools
                require_source_directory repository_checks
                # Admit optional authored roots before inspecting the engine.
                for package in nmrnet_inference nmrnet_provider packages; do
                    if [[ -e "$repository_root/$package" || -L "$repository_root/$package" ]]; then
                        require_source_directory "$package"
                        command+=(--mount "type=bind,src=$repository_root/$package,dst=/workspace/$package,readonly")
                    fi
                done
                if [[ ! -f "$repository_root/Makefile" || -L "$repository_root/Makefile" ]]; then
                    printf -v pending_diagnostic '%s%s\n' "$pending_diagnostic" 'Cannot run repository checks: Makefile must be a regular file without a symlink.'
                    exit 2
                fi
                checker_image
                check_input_directory="$repository_root/.work/repository-check-inputs.PRINT"
                if ! "$print_command"; then
                    prepare_directory "$repository_root/.work"
                    if check_input_directory=$(mktemp -d "$repository_root/.work/repository-check-inputs.XXXXXXXX" 2>/dev/null); then :; else
                        preparation_status=$?
                        # Failed stdout cannot grant directory-removal authority.
                        check_input_directory=
                        printf -v pending_diagnostic '%sCannot prepare repository check inputs: private directory creation under %q failed (status %s); no private cause receipt was established.\n' \
                            "$pending_diagnostic" "$repository_root/.work" "$preparation_status"
                        exit "$preparation_status"
                    fi
                    # Git, find and scaffold cleanup are native work too. Keep
                    # their bare-errexit phase and private cause inside the same
                    # owner before granting a worker these existing outputs.
                    if container_native 'repository check input preparation' untimed discard bash -eu -c '
                        repository=$1 destination=$2
                        find "$repository" -mindepth 1 -maxdepth 1 -printf "%y\t%f\0" > "$destination/root-entries"
                        bash "$repository/tools/check_git_inputs.sh" "$repository" "$destination/git"
                    ' bash "$repository_root" "$check_input_directory"; then preparation_status=0; else preparation_status=$?; fi
                    pending_diagnostic+=$inference_diagnostic
                    if ! "$inference_retirement_confirmed" || [[ $inference_cancellation_status != 0 || $preparation_status != 0 ]]; then
                        exit "$preparation_status"
                    fi
                fi
                command+=(--mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/repository_checks,dst=/workspace/repository_checks,readonly"
                    --mount "type=bind,src=$repository_root/Makefile,dst=/workspace/Makefile,readonly"
                    --mount "type=bind,src=$check_input_directory/root-entries,dst=/repository-source-inventory/root-entries,readonly"
                    --mount "type=bind,src=$check_input_directory/git,dst=/workspace/.git,readonly"
                    --mount "type=bind,src=$repository_root/.git/objects,dst=/workspace/.git/objects,readonly"
                    --mount "type=bind,src=$repository_root/.git/modules/nmrnet/objects,dst=/workspace/.git/modules/nmrnet/objects,readonly"
                    --env "NMRNET_EXPECTED_UID=$caller_uid" --env "NMRNET_EXPECTED_GID=$caller_gid")
                for source_input in tests containers requirements artifacts notes docs NMRNetplusplus README.md .gitignore .gitmodules .github GNUmakefile makefile; do
                    if [[ -e "$repository_root/$source_input" || -L "$repository_root/$source_input" ]]; then
                        if [[ -L "$repository_root/$source_input" || ( ! -f "$repository_root/$source_input" && ! -d "$repository_root/$source_input" ) ]]; then
                            printf -v pending_diagnostic '%sCannot check repository source: public input must be a regular file or directory without a symlink: %s\n' "$pending_diagnostic" "$source_input"
                            exit 2
                        fi
                        command+=(--mount "type=bind,src=$repository_root/$source_input,dst=/workspace/$source_input,readonly")
                    fi
                done
                # Git ignores safe.directory in local config. A contained global
                # config admits only these root-owned mount destinations; HOME
                # remains available when source verification scrubs GIT_* vars.
                repository_tests=(discover -s repository_checks -v)
                if [[ "$1" == test/repository/documentation ]]; then
                    repository_tests=(-v repository_checks.test_documentation)
                fi
                if [[ "$1" == test/repository/wheelhouse ]]; then
                    repository_tests=(-v repository_checks.test_wheelhouse_client_config repository_checks.test_geometry_image
                        repository_checks.test_inference_inputs repository_checks.test_engine_metadata_fixture
                        tests.test_guarded_tooling tests.test_provider_core_launcher
                        tests.test_launcher.LauncherTests.test_foreground_process_lane_is_focused_offline_and_reaps_children
                        tests.unit.normative.test_provider_image tests.unit.adversarial.test_provider_image tests.unit.normative.test_delivery_image)
                    command+=(--mount "type=bind,src=$repository_root/requirements/provider-core-build.lock,dst=/input/provider-core-build.lock,readonly")
                fi
                if [[ "$1" == test/repository/native-images ]]; then
                    repository_tests=(-v repository_checks.test_geometry_image repository_checks.test_inference_inputs
                        repository_checks.test_engine_metadata_fixture repository_checks.test_source_closure.ImageSelectorDetectionTests)
                fi
                if [[ "$1" == test/repository/operator-inputs ]]; then
                    repository_tests=(-v repository_checks.test_acquisition_inputs repository_checks.test_operator_inputs_port.OperatorInputTests
                        repository_checks.test_operator_inputs_port.EngineEnvironmentTests tests.unit.normative.test_operator_guidance
                        tests.unit.normative.test_provider_prepare tests.unit.adversarial.test_provider_prepare)
                fi
                if [[ "$1" == test/repository/provider-core ]]; then
                    repository_tests=(-v repository_checks.test_conventions
                        tests.test_provider_core_launcher
                        repository_checks.test_scientific_lint.ImportOrderDetectionTests
                        repository_checks.test_scientific_lint.ScientificLintTests.test_provider_core_import_order
                        repository_checks.quality.test_source_inventory.SourceInventoryUnitTests
                        repository_checks.quality.test_source_inventory.SourceInventoryRepositoryTests.test_host_root_inventory_is_complete_and_visible_to_source_checks
                        repository_checks.quality.test_source_inventory.SourceInventoryRepositoryTests.test_authored_python_roots_are_complete_safe_trees
                        repository_checks.quality.test_source_inventory.SourceInventoryRepositoryTests.test_repository_checker_grants_complete_read_only_source_trees
                        repository_checks.quality.test_make_hygiene
                        repository_checks.quality.test_python_cache_clutter)
                fi
                command+=(--env HOME=/tmp/checker-home "$checker_image_id" bash -eu -c
                    'mkdir -m 700 /tmp/checker-home; printf "%s\n" "[safe]" "directory = /workspace" "directory = /workspace/NMRNetplusplus" > /tmp/checker-home/.gitconfig; exec python3 -B -m unittest "$@"'
                    bash "${repository_tests[@]}")
                ;;
            download|check|inspect|extract)
                archive_mount="type=bind,src=$repository_root/.work/downloads,dst=/downloads"
                if [[ "$1" != download ]]; then archive_mount+=,readonly; fi
                # Module launch keeps the artifact owner and shared guard in one namespace.
                command+=(--mount "type=bind,src=$repository_root/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
                    --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
                    --mount "type=bind,src=$repository_root/artifacts/release.json,dst=/inputs/release.json,readonly"
                    --mount "$archive_mount")
                if [[ "$1" == extract ]]; then
                    command+=(--mount "type=bind,src=$selection,dst=/inputs/selection.json,readonly"
                        --mount "type=bind,src=$repository_root/.work/prepared,dst=/prepared")
                fi
                command+=("$image" python3 -B -m tools.artifacts "$1"
                    --declaration /inputs/release.json --directory /downloads --name "$archive")
                if [[ "$1" == extract ]]; then
                    command+=(--selection /inputs/selection.json --output "/prepared/${archive%.zip}")
                fi
                ;;
            probe)
                prepare_directory "$repository_root/.work"
                probe_directory="$repository_root/.work/probe.PRINT"
                if ! "$print_command"; then
                    if probe_directory=$(mktemp -d "$repository_root/.work/probe.XXXXXXXX" 2>/dev/null); then :; else
                        preparation_status=$?
                        probe_directory=
                        printf -v pending_diagnostic '%sCannot prepare probe output: private directory creation under %q failed (status %s); no private cause receipt was established.\n' \
                            "$pending_diagnostic" "$repository_root/.work" "$preparation_status"
                        exit "$preparation_status"
                    fi
                fi
                command+=(--mount "type=bind,src=$repository_root/tools/probe.py,dst=/workspace/tools/probe.py,readonly"
                    --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
                    --mount "type=bind,src=$probe_directory,dst=/output"
                    "$image" python3 -B -m tools.probe "$caller_uid" "$caller_gid")
                ;;
        esac
        ;;
    *)
        printf 'Unknown container lane: %s\n' "$1" >&2
        exit 2
        ;;
esac

if "$print_command"; then
    if [[ "$1" == download ]]; then
        "${command[@]}"
    else
        printf '%q ' "${command[@]}"
        printf '\n'
    fi
else
    if [[ "$1" == download ]]; then
        # The acquisition wrapper owns both native and container lifecycles;
        # this caller prepared no Docker configuration or local scratch.
        trap - EXIT HUP INT TERM
        exec "${command[@]}"
    fi
    # The shared owner supplies the launcher's original-signal/two-second
    # contract. There is no second ready/release/status or PID authority here.
    if container_native 'contained lane' untimed stdio "${command[@]}"; then native_status=0; else native_status=$?; fi
    pending_diagnostic+=$inference_diagnostic
    if ! "$inference_retirement_confirmed" || [[ $inference_cancellation_status != 0 || $native_status != 0 ]]; then
        exit "$native_status"
    fi
    if [[ "$1" == probe ]]; then
        if observed_owner="$(stat -c '%u:%g' "$probe_directory/ownership" 2>/dev/null)"; then :; else
            metadata_status=$?
            printf -v pending_diagnostic '%sCannot verify probe output ownership: its metadata could not be read (status %s).\n' "$pending_diagnostic" "$metadata_status"
            exit "$metadata_status"
        fi
        if [[ "$observed_owner" != "$caller_uid:$caller_gid" ]]; then
            printf -v pending_diagnostic '%sProbe output ownership is %s; expected %s:%s.\n' "$pending_diagnostic" "$observed_owner" "$caller_uid" "$caller_gid"
            exit 1
        fi
        if container_native 'probe ownership reporting' untimed stdio bash -eu -c \
            'printf "Probe output ownership: %s\n" "$1"' bash "$observed_owner"; then native_status=0; else native_status=$?; fi
        pending_diagnostic+=$inference_diagnostic
        exit "$native_status"
    fi
fi
