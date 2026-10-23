"""Service operations refuse unrelated configuration and preserve running work."""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.unit.provider_service_fakes import ServiceFixture


class ProviderServiceOperationsAdversarialTests(unittest.TestCase):
    def test_two_instances_cannot_both_take_the_same_runs_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs, runtime = root / "runs", root / "runtime"
            runs.mkdir(mode=0o700)
            runtime.mkdir(mode=0o700)
            environment = dict(os.environ, TEST_RUNS=str(runs), TEST_RUNTIME=str(runtime), TEST_ROOT=str(root))
            result = subprocess.run(
                [
                    "bash",
                    "-euc",
                    """
source tools/provider.sh
NMRNET_RUNS_ROOT_INPUT=$TEST_RUNS RUNTIME_DIRECTORY=$TEST_RUNTIME provider_host_diagnostic=
NMRNET_INSTALLATION_ROOT_INPUT= INVOCATION_ID=00000000000000000000000000000001
(
    provider_host_service_installation
    touch "$TEST_ROOT/ready"
    while [[ ! -e $TEST_ROOT/stop ]]; do sleep 0.01; done
) &
original=$!
trap 'touch "$TEST_ROOT/stop"; wait "$original"' EXIT
until [[ -f $TEST_ROOT/ready ]]; do sleep 0.01; done
INVOCATION_ID=00000000000000000000000000000002
if provider_host_service_installation; then exit 97; fi
[[ $provider_host_diagnostic == *'Another service owns this runs root'* ]]
[[ ! -e $TEST_RUNS/current ]]
""",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_unsafe_instance_or_profile_is_refused_before_manager_calls(self):
        for fault in ("instance", "permissions", "unknown-variable", "symlink"):
            with self.subTest(fault=fault):
                fixture = ServiceFixture()
                self.addCleanup(fixture.close)
                if fault == "instance":
                    fixture.environment["NMRNET_UNIT_INPUT"] = "../unrelated"
                elif fault == "permissions":
                    fixture.profile.chmod(0o644)
                elif fault == "unknown-variable":
                    fixture.profile.write_text("LD_PRELOAD=/private/injected.so\n")
                else:
                    link = fixture.root / "linked.env"
                    link.symlink_to(fixture.profile)
                    fixture.environment["NMRNET_SERVICE_CONFIG_INPUT"] = str(link)
                result = fixture.run("install")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(fixture.calls(), [])

    def test_active_instance_cannot_be_reconfigured_and_unowned_template_is_preserved(self):
        for fault in ("active", "foreign-template"):
            with self.subTest(fault=fault):
                fixture = ServiceFixture()
                self.addCleanup(fixture.close)
                if fault == "active":
                    fixture.environment["TEST_ACTIVE"] = "true"
                else:
                    unit = fixture.home / ".config/systemd/user/nmrnet-provider@.service"
                    unit.parent.mkdir(parents=True)
                    unit.write_text("another owner\n")
                result = fixture.run("install")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(any(b"daemon-reload" in call for call in fixture.calls()))
                if fault == "foreign-template":
                    self.assertEqual(unit.read_text(), "another owner\n")

    def test_service_root_requires_real_invocation_and_never_adopts_prior_nonempty_evidence(self):
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
NMRNET_RUNS_ROOT_INPUT=$TEST_RUNS RUNTIME_DIRECTORY=$TEST_RUNTIME provider_host_diagnostic=
INVOCATION_ID=invalid NMRNET_INSTALLATION_ROOT_INPUT=
if provider_host_service_installation; then exit 97; fi
INVOCATION_ID=00000000000000000000000000000001
provider_host_service_installation
provider_host_root=$NMRNET_INSTALLATION_ROOT_INPUT
mkdir -m 700 -- "$provider_host_root"
printf 'unsettled receipt' > "$provider_host_root/retained"
if provider_host_initialize; then exit 98; fi
[[ $(cat "$provider_host_root/retained") == 'unsettled receipt' ]]
exec {provider_host_service_lock}<&-
ln -s -- "$provider_host_root" "$TEST_RUNS/current"
NMRNET_INSTALLATION_ROOT_INPUT= INVOCATION_ID=00000000000000000000000000000002
if provider_host_service_installation; then exit 99; fi
[[ $provider_host_diagnostic == *'retirement is unconfirmed'* ]]
[[ ! -e $TEST_RUNS/$INVOCATION_ID ]]
[[ ! -e $TEST_RUNTIME/current ]]
""",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
