#!/usr/bin/env bash
# Prepare the small reference datasets offline, with one dedicated output grant.
set -euo pipefail

readonly repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly caller_uid="$(id -u)"
readonly caller_gid="$(id -g)"
print_command=false
if [[ "${1:-}" == --print ]]; then print_command=true; shift; fi
if [[ $# != 1 || "$1" != prepare ]]; then
    printf '%s\n' 'Usage: bash tools/baseline.sh [--print] prepare' >&2
    exit 2
fi
if [[ "$caller_uid" == 0 || "$caller_gid" == 0 ]]; then
    printf '%s\n' 'Baseline preparation requires a nonzero host UID and GID.' >&2
    exit 2
fi
if [[ "$repository_root" == *','* || "$repository_root" == *$'\n'* || "$repository_root" == *$'\r'* ]]; then
    printf '%s\n' 'Repository path cannot contain commas or line breaks in Docker bind mounts.' >&2
    exit 2
fi
for name in tools artifacts .work .work/inference .work/prepared .work/prepared/data .work/prepared/weight; do
    if [[ -L "$repository_root/$name" || ! -d "$repository_root/$name" ]]; then
        printf 'Cannot prepare baseline: %s must be an existing directory without a symlink.\n' "$name" >&2
        exit 2
    fi
done
for name in tools/artifacts.py tools/baseline_prepare.py artifacts/release.json \
            artifacts/data.selection.json artifacts/weight.selection.json .work/inference/image-id \
            .work/prepared/data/provenance.json .work/prepared/weight/provenance.json; do
    if [[ -L "$repository_root/$name" || ! -f "$repository_root/$name" ]]; then
        printf 'Cannot prepare baseline: %s must be a regular file without a symlink.\n' "$name" >&2
        exit 2
    fi
done
image_id="$(cat -- "$repository_root/.work/inference/image-id")"
if [[ ! "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
    printf '%s\n' 'Cannot prepare baseline: image-id does not contain an immutable image ID.' >&2
    exit 2
fi
output_parent="$repository_root/.work/baseline"
if [[ -L "$output_parent" || ( -e "$output_parent" && ! -d "$output_parent" ) ]]; then
    printf '%s\n' 'Cannot prepare baseline: .work/baseline must be a directory without a symlink.' >&2
    exit 2
fi
if [[ -e "$output_parent/prepared" || -L "$output_parent/prepared" ]]; then
    printf '%s\n' 'Cannot prepare baseline: .work/baseline/prepared already exists; the existing result is preserved.' >&2
    exit 2
fi
if ! "$print_command"; then mkdir -p -- "$output_parent"; fi
command=(env -u DOCKER_HOST -u DOCKER_CONTEXT -u DOCKER_TLS_VERIFY -u DOCKER_CERT_PATH
    docker --host unix:///var/run/docker.sock run --rm --network none --pull never --read-only
    --user "$caller_uid:$caller_gid" --cap-drop ALL --security-opt no-new-privileges:true
    --cpus 2 --memory 2g --memory-swap 2g --pids-limit 64
    --tmpfs /tmp:rw,nosuid,nodev,noexec,size=256m --workdir /workspace)
for name in artifacts.py baseline_prepare.py; do
    command+=(--mount "type=bind,src=$repository_root/tools/$name,dst=/workspace/tools/$name,readonly")
done
for name in release.json data.selection.json weight.selection.json; do
    command+=(--mount "type=bind,src=$repository_root/artifacts/$name,dst=/input/$name,readonly")
done
for name in data weight; do
    command+=(--mount "type=bind,src=$repository_root/.work/prepared/$name,dst=/prepared/$name,readonly")
done
command+=(--mount "type=bind,src=$output_parent,dst=/baseline"
    "$image_id" python3 -B -m tools.baseline_prepare
    --prepared-root /prepared --declaration /input/release.json
    --data-selection /input/data.selection.json --dictionary-selection /input/weight.selection.json
    --output /baseline/prepared --count 2 --max-atoms 128 --max-tokens 130)
if "$print_command"; then
    printf '%q ' "${command[@]}"; printf '\n'
else
    exec "${command[@]}"
fi
