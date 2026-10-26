"""Real Make and cooperative waits, with preparation effects recorded locally."""

from tools.container_guard import require_container

require_container()

import os
import shutil
import subprocess
import tempfile
from pathlib import Path


class PreparationFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "repository"
        (self.root / "tools").mkdir(parents=True)
        shutil.copyfile("Makefile", self.root / "Makefile")
        for name in (
            "provider_prepare.sh",
            "wheelhouse_path.sh",
            "acquisition_interface.sh",
            "inference_native.sh",
            "operator_guidance.sh",
        ):
            shutil.copyfile(Path("tools") / name, self.root / "tools" / name)
        self.journal = self.root / "calls"
        self.environment = os.environ | {"TEST_PREPARE_ROOT": str(self.root), "TEST_PREPARE_JOURNAL": str(self.journal)}
        for owner in ("wheelhouse.sh", "inference_image.sh", "container.sh"):
            (self.root / "tools" / owner).write_text(r"""#!/bin/bash
set -eu
root=$TEST_PREPARE_ROOT
phase=${0##*/}:$*
printf '%s\0' "$phase" "$NMRNET_WHEELHOUSE_INPUT" "$NMRNET_WLAN_INTERFACE_INPUT" "${NMRNET_ARCHIVE_INPUT-}" >> "$TEST_PREPARE_JOURNAL"
if [[ ${TEST_WAIT_PHASE-} == "$phase" ]]; then
    trap 'sleep 0.1; printf settled > "$root/settled"; exit 143' TERM INT
    printf ready > "$root/ready"
    sleep 2
fi
if [[ ${TEST_FAIL_PHASE-} == "$phase" ]]; then exit 37; fi
if [[ $phase == 'container.sh:extract' ]]; then
    mkdir -p "$root/.work/prepared/${NMRNET_ARCHIVE_INPUT%.zip}"
    printf '{"fixture":"creation-only"}\n' > "$root/.work/prepared/${NMRNET_ARCHIVE_INPUT%.zip}/provenance.json"
fi
source "$root/tools/operator_guidance.sh"
inference_native() { shift 3; "$@"; }
report_next_actions 'NEXT_FROM_CHILD'
""")

    def close(self):
        self.temporary.cleanup()

    def command(self, selection):
        return [
            "make",
            "--no-print-directory",
            "-C",
            str(self.root),
            "provider/prepare",
            f"WHEELHOUSE={selection}",
            "WLAN_INTERFACE=lo",
            "SHARED_RUNTIME_IMAGE=sha256:" + "a" * 64,
        ]

    def run(self, selection):
        return subprocess.run(
            self.command(selection), env=self.environment, text=True, capture_output=True, timeout=15, check=False
        )

    def calls(self):
        fields = self.journal.read_bytes().split(b"\0")[:-1]
        return [tuple(value.decode() for value in fields[index : index + 4]) for index in range(0, len(fields), 4)]
