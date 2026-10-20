"""The foreground host consumes admitted data without evaluating shell syntax."""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit.worker_host_fakes import HostFixture


class ProviderHostNormativeTests(unittest.TestCase):
    def test_aggregate_above_one_mib_reaches_the_capture_helper_unchanged(self):
        host = HostFixture()
        self.addCleanup(host.close)
        host.environment.update(FAKE_POSTURE_STATE="created", FAKE_POSTURE_RUNNING="false")
        result = host.run(r"""
source tools/provider.sh
umask 077
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components"
declare -A provider_host_ids=()
provider_host_stop_requested=false provider_host_diagnostic=
controller_image_id=$FAKE_IMAGE
provider_host_grants=(--mount type=bind,src=/private/input,dst=/private/input,readonly)
maximum=2097152
provider_host_bounds "$maximum" 4 10
provider_host_create capture none nmrnet_provider.launch_capture --maximum-bytes "$maximum"
provider_host_remove capture
[[ ${#provider_host_ids[@]} == 0 ]]
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        create = host.commands()[0]
        self.assertEqual(create[create.index(b"--maximum-bytes") + 1], b"2097152")
        self.assertIn(b"nmrnet_provider.launch_capture", create)
        self.assertEqual([row[0] for row in host.commands()], [b"create", b"inspect", b"start", b"stop", b"inspect", b"rm"])

    def test_complete_retired_helper_handoff_preserves_each_field_as_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff"
            values = [
                b"/private/source",
                b"provider.toml",
                b"a" * 64,
                b"api.example:443",
                b"model.example:443\nother.example:8443",
                b"store.example:443",
                b"/private/state",
                b"31",
                b"92",
                b"13",
                b"7",
                b"2",
                b"17",
                b"203",
                b"4",
                b"11",
                b"5",
            ]
            path.write_bytes(b"\0".join(values) + b"\0")
            path.chmod(0o600)
            result = subprocess.run(
                [
                    "bash",
                    "-euc",
                    r"""
source tools/provider.sh
declare -A provider_host_ids=()
provider_host_capture_status=0
provider_host_adopt_plan "$1"
printf '%s\0' "${provider_host_fields[@]}"
""",
                    "host-proof",
                    str(path),
                ],
                env=os.environ,
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, path.read_bytes())
