"""Record service-manager requests while running the real host entrypoint."""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import tempfile
from pathlib import Path


class ServiceFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.executables = tempfile.TemporaryDirectory(dir="/test-bin")
        self.bin = Path(self.executables.name)
        self.records = self.root / "records"
        self.records.mkdir()
        self.profile = self.root / "profile.env"
        self.profile.write_text('NMRNET_CONFIG_INPUT="/private/input/provider.toml"\nNMRNET_RESOLVERS_INPUT="8.8.8.8 1.1.1.1"\n')
        self.profile.chmod(0o600)
        systemctl = self.bin / "systemctl"
        systemctl.write_text("""#!/bin/bash
set -eu
record=$(mktemp "$TEST_RECORDS/call.XXXXXXXX")
printf '%s\\0' "$@" > "$record"
if [[ $* == *--property=ActiveState* && $* == *--value* ]]; then
    if [[ ${TEST_ACTIVE:-false} == true ]]; then printf 'active\\n'; else printf 'inactive\\n'; fi
fi
""")
        systemctl.chmod(0o700)
        self.environment = {
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "HOME": str(self.home),
            "TEST_RECORDS": str(self.records),
            "NMRNET_UNIT_INPUT": "local",
            "NMRNET_SERVICE_CONFIG_INPUT": str(self.profile),
        }

    def close(self):
        self.executables.cleanup()
        self.temporary.cleanup()

    def run(self, operation):
        return subprocess.run(
            ["bash", "tools/provider_service.sh", operation],
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def calls(self):
        return [path.read_bytes().split(b"\0")[:-1] for path in self.records.iterdir()]
