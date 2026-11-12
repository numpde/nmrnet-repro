#!/usr/bin/env bash
# Test-only native port: real source capture, modeled build/inspect responses.
# No engine is invoked. The real owner must ignore progress IDs and admit only
# the completed private ID file before preserving/replacing its prior record.
set -euo pipefail
repository_root=$1 build_case=$2
wheelhouse_directory=$repository_root/.work/wheelhouse
image_input_directory= image=PINNED_BASE print_command=false
pending_diagnostic= inference_diagnostic= inference_retirement_confirmed=true
inference_cancellation_status=0
engine=(fixture)
caller_uid=$(id -u)
caller_gid=$(id -g)
identity=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
other=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
source "$repository_root/tools/provider_image.sh"
source "$repository_root/tools/operator_guidance.sh"

container_native() {
    local operation=$1
    shift 3
    printf '%s\n' "$operation" >> "$repository_root/.work/provider/native-calls"
    case "$operation" in
        'controller base image admission') inference_result=$identity ;;
        'controller image input capture') "$@" ;;
        'controller runtime wheel selection')
            selector=("${@:7}")
            expected_selector=(fixture run --rm --pull never --network none --read-only
                --user 65532:65532 --cap-drop ALL --security-opt no-new-privileges:true
                --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m --workdir /workspace --env NMRNET_IN_CONTAINER=1
                --mount "type=bind,src=$image_input_directory/context/tools/wheelhouse.py,dst=/workspace/tools/wheelhouse.py,readonly"
                --mount "type=bind,src=$image_input_directory/context/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly"
                --mount "type=bind,src=$image_input_directory/context/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly"
                --mount "type=bind,src=$image_input_directory/context/requirements/provider-core-install.lock,dst=/input/requirements.lock,readonly"
                --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" "$image"
                python3 -B -m tools.wheelhouse report --lock /input/requirements.lock --lock-sha256 "$lock_hash" --pool /wheelhouse)
            [[ ${#selector[@]} == ${#expected_selector[@]} ]] || return 99
            for index in "${!expected_selector[@]}"; do [[ ${selector[$index]} == "${expected_selector[$index]}" ]] || return 99; done
            python3 -I -B - "$6" "$wheelhouse_directory/providerfixture-1.0-py3-none-any.whl" <<'PY' || return $?
import hashlib, json, pathlib, sys
wheel = pathlib.Path(sys.argv[2])
report = {"version": "1", "install": [{"download_info": {
    "url": "file:///wheelhouse/" + wheel.name,
    "archive_info": {"hashes": {"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}},
}}]}
pathlib.Path(sys.argv[1]).write_text(json.dumps(report))
PY
            ;;
        'controller image build')
            build_arguments=("${@:11}")
            expected_build=(fixture build --builder default --progress plain --network none --pull=false
                --iidfile "$image_input_directory/built-image-id" --build-arg "PROVIDER_INPUTS=$inputs_digest"
                --build-arg "PROVIDER_WHEELS=$stream_selection" --file containers/provider/Dockerfile -)
            [[ ${#build_arguments[@]} == ${#expected_build[@]} ]] || return 99
            for index in "${!expected_build[@]}"; do [[ ${build_arguments[$index]} == "${expected_build[$index]}" ]] || return 99; done
            python3 -I -S -B "$stream_tool" stream --pool "$wheelhouse_directory" \
                --source-allowlist "$stream_sources" --manifest "$stream_manifest" > "$image_input_directory/stream.tar" || return $?
            python3 -I -S -B - "$image_input_directory/stream.tar" "$stream_sources" "$stream_manifest" "$wheelhouse_directory" <<'PY' || return $?
import hashlib, json, pathlib, sys, tarfile
archive, sources_path, manifest_path, pool = map(pathlib.Path, sys.argv[1:])
sources = json.loads(sources_path.read_text())
wheels = json.loads(manifest_path.read_text())
assert len(wheels) == 1
assert wheels[0]["directory"] == "wheelhouse/runtime"
assert wheels[0]["filename"] == "providerfixture-1.0-py3-none-any.whl"
assert wheels[0]["sha256"] == hashlib.sha256((pool / wheels[0]["filename"]).read_bytes()).hexdigest()
expected = {item["destination"]: pathlib.Path(item["path"]).read_bytes() for item in sources}
expected.update({item["directory"] + "/" + item["filename"]: (pool / item["filename"]).read_bytes() for item in wheels})
with tarfile.open(archive) as stream:
    members = stream.getmembers()
    assert len(members) == len(expected)
    assert {member.name for member in members} == set(expected)
    for member in members:
        assert member.isfile()
        assert hashlib.sha256(stream.extractfile(member).read()).digest() == hashlib.sha256(expected[member.name]).digest()
PY
            printf 'Progress contains misleading ID %s\n' "$other"
            case "$build_case" in
                missing) : ;;
                link) ln -s -- "$repository_root/.work/provider/image-id" "$image_input_directory/built-image-id" ;;
                fifo) mkfifo -- "$image_input_directory/built-image-id" ;;
                nul) printf '%s\0' "$identity" > "$image_input_directory/built-image-id" ;;
                trailing) printf '%s\r' "$identity" > "$image_input_directory/built-image-id" ;;
                oversized) printf '%s\nextra' "$identity" > "$image_input_directory/built-image-id" ;;
                malformed) printf '%s\n' "${identity^^}" > "$image_input_directory/built-image-id" ;;
                newline) printf '%s\n' "$identity" > "$image_input_directory/built-image-id" ;;
                *) printf '%s' "$identity" > "$image_input_directory/built-image-id" ;;
            esac
            if [[ $build_case == failed ]]; then inference_diagnostic='Original modeled RUN failure retained.'; return 7; fi
            if [[ $build_case == unconfirmed ]]; then inference_retirement_confirmed=false; return 2; fi
            ;;
        'controller built-image admission')
            inference_result=$identity
            if [[ $build_case == inspect ]]; then inference_result=$other; fi
            ;;
        'controller image-input admission')
            inference_result=$(sha256sum -- "$image_input_directory/inputs")
            inference_result=sha256:${inference_result%% *}
            if [[ $build_case == label ]]; then inference_result=$other; fi
            ;;
        'controller wheel-input admission')
            inference_result=$stream_selection
            if [[ $build_case == wheel-label ]]; then inference_result=$other; fi
            ;;
        'controller build reporting') "$@" ;;
        *) printf 'Unexpected native operation\n' >&2; return 99 ;;
    esac
}

# This fixture models finite engine receipts while exercising the committed
# source capture owner's admission and cleanup behavior.
inference_native() {
    local operation=$1
    shift 3
    inference_retirement_confirmed=true
    inference_cancellation_status=0
    inference_diagnostic=
    if [[ $operation == 'stopped container diagnostics' ]]; then
        [[ $# == 5 && $1 == fixture && $2 == logs && $3 == --tail && $4 == 80 \
            && $5 == dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd ]]
        return $?
    fi
    local receipts=$6 lifecycle=$7
    local identifier=dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd
    (
        umask 077
        : > "$receipts/$lifecycle.stderr"
        case "$operation" in
            'worker create')
                printf '%s\n' "$identifier" > "$receipts/worker.cid"
                printf '%s\n' "$identifier" > "$receipts/create.stdout" ;;
            'worker inspect')
                printf '%s exited false 0 false\n' "$identifier" > "$receipts/inspect.stdout" ;;
            'worker stop'|'worker remove')
                printf '%s\n' "$identifier" > "$receipts/$lifecycle.stdout" ;;
            *) printf 'Unexpected native lifecycle operation: %s\n' "$operation" >&2; return 99 ;;
        esac
    )
}

build_provider_image
