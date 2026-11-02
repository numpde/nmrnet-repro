#!/usr/bin/env bash
# Prepare target-selected wheels and committed source for the shared streamer.

prepare_streamed_inputs() {
    local description=$1 inputs_digest=$2 item role lock lock_hash tool_line tool_url tool_name tool_hash observed status
    shift 2
    local -a manifests=()
    tool_line=$(grep '^https://github.com/numpde/nmr-provider-tools/releases/download/' "$image_input_directory/context/requirements/wheelhouse-tool-assets.in")
    tool_url=${tool_line%%#sha256=*}
    tool_hash=${tool_line##*#sha256=}
    tool_name=${tool_url##*/}
    if [[ ! $tool_name =~ ^wheelhouse-tool-[0-9]+\.[0-9]+\.[0-9]+\.pyz$ || ! $tool_hash =~ ^[0-9a-f]{64}$ ]]; then
        printf -v pending_diagnostic 'Cannot build %s: the shared wheelhouse tool pin is invalid.\n' "$description"; exit 2
    fi
    stream_tool="$wheelhouse_directory/$tool_name"
    if ! observed=$(sha256sum -- "$stream_tool" 2>/dev/null) || [[ ${observed%% *} != "$tool_hash" ]]; then
        printf -v pending_diagnostic 'Cannot build %s: acquire the pinned shared tool with make packages/shared-tool/wheelhouse.\n' "$description"; exit 2
    fi
    stream_sources="$image_input_directory/sources.json"
    stream_manifest="$image_input_directory/manifest.json"
    python3 -I "$image_input_directory/context/tools/git_stream_sources.py" "$image_input_directory/context" \
        "$image_input_directory/inputs" . "$inputs_digest" > "$stream_sources"
    for item in "$@"; do
        role=${item%%:*}; lock=${item#*:}
        lock_hash=$(sha256sum -- "$image_input_directory/context/requirements/$lock")
        lock_hash=${lock_hash%% *}
        if container_native "$description $role wheel selection" 120 stdio bash -eu -c '
            output=$1; shift
            exec "$@" > "$output"
        ' bash "$image_input_directory/$role.report.json" "${engine[@]}" run --rm --pull never --network none --read-only \
            --user 65532:65532 --cap-drop ALL --security-opt no-new-privileges:true \
            --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m --workdir /workspace --env NMRNET_IN_CONTAINER=1 \
            --mount "type=bind,src=$image_input_directory/context/tools/wheelhouse.py,dst=/workspace/tools/wheelhouse.py,readonly" \
            --mount "type=bind,src=$image_input_directory/context/tools/artifacts.py,dst=/workspace/tools/artifacts.py,readonly" \
            --mount "type=bind,src=$image_input_directory/context/tools/container_guard.py,dst=/workspace/tools/container_guard.py,readonly" \
            --mount "type=bind,src=$image_input_directory/context/requirements/$lock,dst=/input/requirements.lock,readonly" \
            --mount "type=bind,src=$wheelhouse_directory,dst=/wheelhouse,readonly" "$image" \
            python3 -B -m tools.wheelhouse report --lock /input/requirements.lock --lock-sha256 "$lock_hash" --pool /wheelhouse; then :; else
            status=$?; pending_diagnostic+=$inference_diagnostic; exit "$status"
        fi
        python3 -I -S "$stream_tool" report --report "$image_input_directory/$role.report.json" \
            --pool /wheelhouse --directory "wheelhouse/$role" > "$image_input_directory/$role.manifest.json"
        manifests+=("$image_input_directory/$role.manifest.json")
    done
    python3 -I -c 'import json,sys; print(json.dumps(sum((json.load(open(p)) for p in sys.argv[1:]), [])))' \
        "${manifests[@]}" > "$stream_manifest"
    stream_selection=$(python3 -I -S "$stream_tool" identity --manifest "$stream_manifest")
    stream_selection="sha256:$stream_selection"
}

build_streamed_context() {
    local description=$1 budget=$2 disposition=$3
    shift 3
    container_native "$description" "$budget" "$disposition" bash -o pipefail -c '
        tool=$1 pool=$2 sources=$3 manifest=$4; shift 4
        python3 -I -S "$tool" stream --pool "$pool" --source-allowlist "$sources" --manifest "$manifest" | "$@"
    ' bash "$stream_tool" "$wheelhouse_directory" "$stream_sources" "$stream_manifest" "$@"
}
