#!/usr/bin/env bash
# Test-only native port: real source capture, modeled build/inspect responses.
# No engine is invoked. The real owner must ignore progress IDs and admit only
# the completed private ID file before preserving/replacing its prior record.
set -euo pipefail
repository_root=$1 build_case=$2
wheelhouse_directory=$repository_root/.work/wheelhouse
image_input_directory= image=PINNED_BASE print_command=false
pending_diagnostic= inference_diagnostic= inference_retirement_confirmed=true
inference_cancellation_status=0 wheel_capture_retirement_confirmed=true
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
        'controller wheel selection and capture') : ;;
        'controller image build')
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
        'controller build reporting') "$@" ;;
        *) printf 'Unexpected native operation\n' >&2; return 99 ;;
    esac
}

# This fixture models finite engine receipts, while exercising the real capture
# owner's CID admission and retirement. Wheel selection has its own real-pip
# witness in ImageSource.capture; this port only supplies engine lifecycle facts.
inference_native() {
    local operation=$1
    shift 3
    inference_retirement_confirmed=true
    inference_cancellation_status=0
    inference_diagnostic=
    if [[ $operation == 'wheelhouse capture' ]]; then return 0; fi
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
