#!/usr/bin/env bash
# Test-only native port: real source capture, modeled build/inspect responses.
# No engine is invoked. The real owner must ignore progress IDs and admit only
# the completed private ID file before preserving/replacing its prior record.
set -euo pipefail
repository_root=$1 build_case=$2
wheelhouse_directory=$repository_root/.work/wheelhouse
image_input_directory= image=PINNED_BASE print_command=false
pending_diagnostic= inference_diagnostic= inference_retirement_confirmed=true
engine=(fixture)
identity=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
other=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
source "$repository_root/tools/provider_image.sh"

container_native() {
    local operation=$1
    shift 3
    printf '%s\n' "$operation" >> "$repository_root/.work/provider/native-calls"
    case "$operation" in
        'controller base image admission') inference_result=$identity ;;
        'controller image input capture') "$@" ;;
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

build_provider_image
