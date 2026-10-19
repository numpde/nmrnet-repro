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
    # One local-retirement fact gates every host removal. Failed inspection
    # receipts remain operator evidence; a joined client does not erase its cause.
    if "$inference_retirement_confirmed"; then
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
    elif ! "$inference_retirement_confirmed"; then
        # Unknown retirement forbids another owner. Direct reporting keeps its
        # separate blocked-stderr limit; all owned resources remain untouched.
        printf '%s' "$pending_diagnostic" >&2
        printf 'Cannot confirm contained client retirement; owned state retained at %q, %q, %q and %q.\n' "$docker_client_directory" "$image_input_directory" "$check_input_directory" "$probe_directory" >&2
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
    exit "$status"
}

trap cleanup_launcher EXIT
trap 'caller_cancellation=129; exit 129' HUP
trap 'caller_cancellation=130; exit 130' INT
trap 'caller_cancellation=143; exit 143' TERM

case "$1" in
    geometry/image/build)
        if [[ "$caller_uid" == 0 || "$caller_gid" == 0 || "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
            printf -v pending_diagnostic '%s\n' 'RDKit image builds require a nonzero UID/GID and a repository path without commas or line breaks.'
            exit 2
        fi
        for directory in tools requirements containers containers/geometry .work .work/preparation .work/preparation/wheelhouse; do
            require_source_directory "$directory"
        done
        if [[ $(stat -c '%u' -- "$repository_root/.work/preparation") != "$caller_uid" ]]; then
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
        fi
        exit "$download_status"
        ;;
    pull)
        printf '%s\n' 'Base images must be preloaded: Docker daemon pulls cannot be bound by this launcher to WLAN_INTERFACE.' >&2
        printf 'To download this pinned base using Docker default routing, run make packages/base-images/python/download: %s\n' "$image" >&2
        exit 2
        ;;
    test/integration/native-geometry-worker|test/integration/sdf-reader|test/integration/mol-reader|test/integration/rdkit-worker|test/integration/rdkit-runtime|test/unit/packages|test/contract/provider-core|test/integration/provider-core|test/integration/provider-core/prepared-identity|test/unit/provider-core|test/unit/provider-core/normative|test/unit/provider-core/adversarial|test/unit/provider-core/prepared-identity|test|test/unit/provider|test/unit/provider/pdb|test/unit/provider/mol2|test/unit/provider-geometry-worker|test/unit/bond-distances|test/unit/prediction-input|test/unit/baseline-prepare|test/unit/baseline-run|test/unit/baseline-model|test/unit/baseline-launcher|test/repository|test/repository/provider-core|probe|download|check|inspect|extract)
        if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
            printf '%s\n' 'Contained lanes require a nonzero host UID and GID.' >&2
            exit 2
        fi
        if [[ "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
            printf '%s\n' 'Repository path cannot contain commas or line breaks in Docker bind mounts.' >&2
            exit 2
        fi
        if [[ "$1" == download ]]; then
            bash "$repository_root/tools/docker.sh" check-interface
        fi
        if [[ "$1" == download || "$1" == check || "$1" == inspect || "$1" == extract ]]; then
            # Make owns public input origin and hands over only its literal
            # value alias. Ambient ARCHIVE must not select an archive action.
            if [[ ! -v NMRNET_ARCHIVE_INPUT ]] || [[ ! "$NMRNET_ARCHIVE_INPUT" =~ ^[A-Za-z0-9_.-]+\.zip$ ]]; then
                printf '%s\n' 'Select an archive on the make command line, for example make weights/check ARCHIVE=weight.zip.' >&2
                exit 2
            fi
            archive="$NMRNET_ARCHIVE_INPUT"
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
            command+=(run)
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
        if [[ "$1" == test || "$1" == test/unit/provider || "$1" == test/unit/provider-geometry-worker || "$1" == test/unit/bond-distances || "$1" == test/unit/prediction-input || "$1" == test/unit/baseline-prepare || "$1" == test/unit/baseline-run || "$1" == test/unit/baseline-model || "$1" == test/unit/baseline-launcher || "$1" == test/repository || "$1" == test/repository/provider-core ]]; then
            # Native cancellation probes retire whole process groups. An init
            # process reaps orphaned descendants; Python as PID 1 would retain
            # zombies until the fixed 64-process budget was exhausted.
            command+=(--init)
        fi
        case "$1" in
            test/integration/rdkit-worker|test/integration/mol-reader|test/integration/sdf-reader|test/integration/native-geometry-worker)
                for directory in tools tests tests/integration tests/integration/rdkit_runtime requirements .work .work/preparation; do
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
                for directory in tools tests tests/integration tests/integration/rdkit_runtime requirements .work .work/preparation .work/preparation/wheelhouse; do
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
                    --mount "type=bind,src=$repository_root/.work/preparation/wheelhouse,dst=/runtime-deps,readonly"
                    "$image" python3 -B -m tools.rdkit_runtime_tests temporary)
                ;;
            test/unit/packages)
                require_source_directory tools
                require_source_directory tests
                command+=(--init
                    --mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/tests,dst=/workspace/tests,readonly"
                    "$image" python3 -B -m unittest -v tests.test_package_lock tests.test_wheelhouse tests.test_guarded_tooling)
                ;;
            test/contract/provider-core|test/integration/provider-core|test/integration/provider-core/prepared-identity|test/unit/provider-core|test/unit/provider-core/normative|test/unit/provider-core/adversarial|test/unit/provider-core/prepared-identity)
                require_source_directory tools
                require_source_directory packages
                require_source_directory packages/provider-core
                require_source_directory .work
                require_source_directory .work/wheelhouse
                require_source_directory .work/wheelhouse/cpu
                require_source_directory .work/provider-core
                require_source_directory .work/provider-core/wheelhouse
                require_source_directory requirements
                for core_file in requirements/provider-core-build.lock requirements/provider-core.lock .work/wheelhouse/cpu/setuptools-84.0.0-py3-none-any.whl tools/container_guard.py; do
                    if [[ ! -f "$repository_root/$core_file" || -L "$repository_root/$core_file" ]]; then
                        printf -v pending_diagnostic '%sCannot test provider-core: expected a regular build input at %s.\n' "$pending_diagnostic" "$core_file"
                        exit 2
                    fi
                done
                core_suite=unit
                if [[ "$1" == test/contract/provider-core ]]; then core_suite=contract; fi
                if [[ "$1" == test/integration/provider-core || "$1" == test/integration/provider-core/prepared-identity ]]; then
                    core_suite=integration
                    require_source_directory nmrnet_provider
                    require_source_directory nmrnet_inference
                    require_source_directory tests
                    require_source_directory tests/integration
                    require_source_directory tests/integration/provider_core
                    command+=(--mount "type=bind,src=$repository_root/nmrnet_provider,dst=/consumer/nmrnet_provider,readonly"
                        --mount "type=bind,src=$repository_root/tests/integration/provider_core,dst=/consumer-tests,readonly"
                        --mount "type=bind,src=$repository_root/nmrnet_inference,dst=/consumer/nmrnet_inference,readonly"
                        --mount "type=bind,src=$repository_root/tools/container_guard.py,dst=/consumer/tools/container_guard.py,readonly")
                fi
                if [[ "$1" == */normative ]]; then core_suite=unit/normative; fi
                if [[ "$1" == */adversarial ]]; then core_suite=unit/adversarial; fi
                command+=(--init --env PROVIDER_CORE_IN_CONTAINER=1
                    --tmpfs /runtime:rw,nosuid,nodev,exec,size=128m
                    --mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/packages/provider-core,dst=/package,readonly"
                    --mount "type=bind,src=$repository_root/requirements/provider-core-build.lock,dst=/core-build.lock,readonly"
                    --mount "type=bind,src=$repository_root/requirements/provider-core.lock,dst=/core-runtime.lock,readonly"
                    --mount "type=bind,src=$repository_root/.work/provider-core/wheelhouse,dst=/runtime-deps,readonly"
                    --mount "type=bind,src=$repository_root/.work/wheelhouse/cpu/setuptools-84.0.0-py3-none-any.whl,dst=/build-deps/setuptools-84.0.0-py3-none-any.whl,readonly"
                    "$image" python3 -B -m tools.provider_core_tests "$core_suite")
                if [[ "$1" == test/unit/provider-core/prepared-identity ]]; then
                    command+=(--pattern test_prepared_identity.py 'test_execution*.py'
                        test_attempt_transitions.py test_terminal_record.py test_recovery.py)
                fi
                if [[ "$1" == test/integration/provider-core/prepared-identity ]]; then
                    command+=(--pattern test_nmrnet_session.py test_attempt_store.py test_terminal_store.py
                        test_magnet_adaptation.py test_child_retirement.py test_execution.py)
                fi
                ;;
            test|test/unit/provider|test/unit/provider/pdb|test/unit/provider/mol2|test/unit/provider-geometry-worker|test/unit/bond-distances|test/unit/prediction-input|test/unit/baseline-prepare|test/unit/baseline-run|test/unit/baseline-model|test/unit/baseline-launcher)
                require_source_directory tools
                require_source_directory tests
                require_source_directory nmrnet_inference
                require_source_directory nmrnet_provider
                command+=(--mount "type=bind,src=$repository_root/tools,dst=/workspace/tools,readonly"
                    --mount "type=bind,src=$repository_root/tests,dst=/workspace/tests,readonly"
                    --mount "type=bind,src=$repository_root/nmrnet_inference,dst=/workspace/nmrnet_inference,readonly"
                    --mount "type=bind,src=$repository_root/nmrnet_provider,dst=/workspace/nmrnet_provider,readonly"
                    "$image" python3 -B -m unittest discover -s tests)
                if [[ "$1" == test ]]; then
                    command+=(-v)
                elif [[ "$1" == test/unit/provider ]]; then
                    command+=(-p "test_provider_*.py" -v)
                elif [[ "$1" == test/unit/provider/pdb || "$1" == test/unit/provider/mol2 ]]; then
                    command+=(-p "test_provider_${1##*/}.py" -v)
                elif [[ "$1" == test/unit/provider-geometry-worker ]]; then
                    command+=(-p test_provider_geometry_worker.py -v)
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
            test/repository|test/repository/provider-core)
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
                for source_input in tests containers requirements artifacts notes NMRNetplusplus README.md .gitignore .gitmodules .github GNUmakefile makefile; do
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
                if [[ "$1" == test/repository/provider-core ]]; then
                    repository_tests=(-v repository_checks.test_conventions
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
