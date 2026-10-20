"""Recording engine for host ownership tests; it creates no containers."""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import tempfile
from pathlib import Path

TOKEN = "a" * 32
IMAGE = "sha256:" + "b" * 64
CID = "c" * 64

ENGINE = r"""#!/usr/bin/env bash
set -euo pipefail
if [[ ! -d $FAKE_STATE/commands ]]; then mkdir -- "$FAKE_STATE/commands"; fi
sequence=0
if [[ -f $FAKE_STATE/sequence ]]; then IFS= read -r sequence < "$FAKE_STATE/sequence"; fi
sequence=$((sequence + 1))
printf '%s\n' "$sequence" > "$FAKE_STATE/sequence"
printf -v record '%06d' "$sequence"
# Keep every argv without growing one file past the real command owner's 4KiB
# per-file limit. The production command's output limits remain unchanged.
printf '%s\0' "$@" > "$FAKE_STATE/commands/$record"
operation=$1
shift
case "$operation" in
    create)
        cidfile= memory=
        while (( $# )); do
            case "$1" in
                --cidfile) cidfile=$2; shift ;;
                --memory) memory=$2; shift ;;
            esac
            shift
        done
        printf '%s\n' "$FAKE_CID" > "$cidfile"
        printf '%s\n' "$memory" > "$FAKE_STATE/memory"
        if [[ ${FAKE_RETIRE_DURING_CREATE-} == yes ]]; then
            printf 'retire %s\n' "$FAKE_TOKEN" > "$FAKE_ROOT/requests/$FAKE_TOKEN/retire"
        fi
        if [[ ${FAKE_MISMATCH-} == yes ]]; then printf '%064d\n' 0; else printf '%s\n' "$FAKE_CID"; fi
        ;;
    start) printf '%s\n' "$FAKE_CID" ;;
    inspect)
        if [[ "$*" == *State.ExitCode* ]]; then
            if [[ -f $FAKE_STATE/controller-signalled && ${FAKE_CONTROLLER_STAYS_RUNNING-no} != yes ]]; then
                printf '%s|exited|false|%s\n' "$FAKE_CID" "$FAKE_CONTROLLER_EXIT"
            else printf '%s|running|true|0\n' "$FAKE_CID"; fi
        elif [[ "$*" == *HostConfig* ]]; then
            IFS= read -r memory < "$FAKE_STATE/memory"
            if [[ $memory == 4g ]]; then bytes=4294967296; else bytes=536870912; fi
            printf '%s|%s|%s|%s|true|none|false|%s|%s|64|2000000000|["ALL"]|["no-new-privileges:true"]|%s:%s|no\n' \
                "$FAKE_CID" "$FAKE_IMAGE" "${FAKE_POSTURE_STATE-running}" "${FAKE_POSTURE_RUNNING-true}" "$bytes" "$bytes" "$(id -u)" "$(id -g)"
        else printf '%s exited false 0 false\n' "$FAKE_CID"; fi
        ;;
    kill) printf 'TERM\n' > "$FAKE_STATE/controller-signalled" ;;
    stop) exit "${FAKE_STOP_STATUS-0}" ;;
    rm) printf '%s\n' "$FAKE_CID" ;;
    *) exit 97 ;;
esac
"""


class HostFixture:
    def __init__(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)
        self.allocation_root = self.root / "allocations"
        self.engine = self.root / "engine.sh"
        self.engine.write_text(ENGINE)
        self.environment = dict(os.environ)
        self.environment.update(
            FAKE_STATE=str(self.root), FAKE_ROOT=str(self.allocation_root), FAKE_TOKEN=TOKEN, FAKE_IMAGE=IMAGE, FAKE_CID=CID
        )

    def close(self):
        self.scratch.cleanup()

    def program(self, body):
        return (
            r"""
set -euo pipefail
source tools/inference_native.sh
source tools/worker_retirement.sh
source tools/provider_workers.sh
engine=(bash "$FAKE_STATE/engine.sh")
repository_root=$PWD
provider_worker_source_root=$PWD provider_worker_prepared_root=$PWD/.work/prepared
scientific_image_id=$FAKE_IMAGE geometry_image_id=$FAKE_IMAGE
provider_worker_accept_seconds=2 provider_worker_io_seconds=2 provider_source_io_seconds=2 provider_generation_io_seconds=5
provider_worker_seed=17 provider_worker_max_iterations=80 provider_worker_embedding_seconds=1
inference_retirement_confirmed=true inference_cancellation_status=0 inference_diagnostic=
worker_retirement_diagnostic=
"""
            + body
        )

    def run(self, body):
        return subprocess.run(
            ["bash", "-c", self.program(body)], env=self.environment, capture_output=True, text=True, timeout=10, check=False
        )

    def commands(self):
        return [path.read_bytes().split(b"\0")[:-1] for path in sorted((self.root / "commands").iterdir())]
