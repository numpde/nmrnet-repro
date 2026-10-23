"""A persistent user service delegates execution and retirement to the host owner."""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit.provider_service_fakes import ServiceFixture


class ProviderServiceOperationsTests(unittest.TestCase):
    def test_install_preserves_literal_inputs_and_keeps_start_and_enable_separate(self):
        fixture = ServiceFixture()
        self.addCleanup(fixture.close)
        fixture.profile.write_text('NMRNET_CONFIG_INPUT="/private/$(touch sentinel)/provider.toml"\n')
        result = fixture.run("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = fixture.home / ".config/nmrnet-repro/local.env"
        self.assertTrue(installed.read_bytes().startswith(fixture.profile.read_bytes()))
        self.assertEqual(installed.stat().st_mode & 0o777, 0o600)
        self.assertEqual((installed.parent.stat().st_mode & 0o777), 0o700)
        self.assertFalse(Path("sentinel").exists())
        self.assertFalse(any(b"start" in call or b"enable" in call for call in fixture.calls()))
        for operation, expected in (("start", b"start"), ("stop", b"stop"), ("enable", b"enable"), ("disable", b"disable")):
            with self.subTest(operation=operation):
                result = fixture.run(operation)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(any(expected in call and b"nmrnet-provider@local.service" in call for call in fixture.calls()))
        self.assertTrue(any(b"stop" in call and b"--no-block" in call for call in fixture.calls()))
        self.assertTrue(any(b"start" in call and b"--job-mode=fail" in call for call in fixture.calls()))

    def test_each_invocation_uses_fresh_evidence_without_changing_durable_attempt_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs, runtime = root / "runs", root / "runtime"
            runs.mkdir(mode=0o700)
            runtime.mkdir(mode=0o700)
            environment = dict(os.environ, TEST_RUNS=str(runs), TEST_RUNTIME=str(runtime))
            result = subprocess.run(
                [
                    "bash",
                    "-euc",
                    """
source tools/provider.sh
NMRNET_RUNS_ROOT_INPUT=$TEST_RUNS RUNTIME_DIRECTORY=$TEST_RUNTIME
for invocation in 00000000000000000000000000000001 00000000000000000000000000000002; do
    INVOCATION_ID=$invocation NMRNET_INSTALLATION_ROOT_INPUT= provider_host_diagnostic=
    provider_host_service_installation
    [[ $NMRNET_INSTALLATION_ROOT_INPUT == "$TEST_RUNS/$invocation" ]]
    provider_host_root=$NMRNET_INSTALLATION_ROOT_INPUT
    mkdir -m 700 -- "$provider_host_root"
    printf 'original evidence' > "$provider_host_root/retained"
    provider_host_service_locator
    printf 'retired\n' > "$provider_host_root/host-retired"
    chmod 600 -- "$provider_host_root/host-retired"
    exec {provider_host_service_lock}<&-
done
[[ $(readlink -f "$TEST_RUNTIME/current") == "$TEST_RUNS/$invocation" ]]
""",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual({path.name for path in runs.iterdir()}, {"0" * 31 + "1", "0" * 31 + "2", "current"})
            self.assertEqual((runs / ("0" * 31 + "1") / "retained").read_text(), "original evidence")

    def test_unit_retains_original_host_until_drain_without_automatic_retry(self):
        unit = Path("deployment/nmrnet-provider@.service").read_text()
        self.assertIn("ExecStart=/bin/bash ${NMRNET_REPOSITORY_ROOT}/tools/provider.sh", unit)
        self.assertIn("KillMode=mixed\nSendSIGKILL=no\nTimeoutStopSec=infinity\nRestart=no", unit)
        self.assertIn("RuntimeDirectoryPreserve=yes", unit)
        self.assertIn("WantedBy=default.target", unit)
