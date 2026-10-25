"""Finite qualification helpers preserve exact component grants and cleanup."""

from tools.container_guard import require_container

require_container()

import unittest

from tests.unit.worker_host_fakes import HostFixture


class ProviderHostNormativeTests(unittest.TestCase):
    def test_service_component_has_fixed_resources_and_isolation(self):
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
grants=() command=(/opt/provider/environment/bin/python -I -B -m nmrnet_provider.process)
provider_host_create helper none "$FAKE_IMAGE" grants command false
provider_host_remove helper
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        create = host.commands()[0]
        for option, value in ((b"--memory", b"512m"), (b"--memory-swap", b"512m"), (b"--pids-limit", b"64")):
            self.assertEqual(create[create.index(option) + 1], value)
        self.assertIn(b"/tmp:rw,nosuid,nodev,noexec,size=64m", create)
        self.assertIn(b"--read-only", create)
        self.assertIn(b"ALL", create)

    def test_helper_images_commands_and_grants_use_the_same_private_launch_owner(self):
        for network, interpreter, script in (
            ("fixture-network", "/opt/provider/environment/bin/python", "model_peer.py"),
            ("none", "/opt/provider/environment/bin/python", "stage.py"),
            ("host", "/usr/local/bin/python", "api_prediction.py"),
        ):
            with self.subTest(script=script):
                host = HostFixture()
                self.addCleanup(host.close)
                host.environment.update(
                    FAKE_POSTURE_STATE="created",
                    FAKE_POSTURE_RUNNING="false",
                    FAKE_IMAGE="sha256:" + "d" * 64,
                    FAKE_NETWORK=network,
                    FAKE_INTERPRETER=interpreter,
                    FAKE_SCRIPT=script,
                    FAKE_MOUNTS='[{"Destination":"/input/case.json","RW":false}]',
                )
                result = host.run(r"""
source tools/provider.sh
umask 077
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components"
declare -A provider_host_ids=()
provider_host_stop_requested=false provider_host_diagnostic=
helper_grants=(--env NMRNET_IN_CONTAINER=1 --env PYTHONPATH=/fixture --mount 'type=bind,src=/private/case file,dst=/input/case.json,readonly')
helper_command=("$FAKE_INTERPRETER" -B "/fixture/$FAKE_SCRIPT" --literal '$(touch /tmp/never-execute); literal value')
provider_host_create helper "$FAKE_NETWORK" "$FAKE_IMAGE" helper_grants helper_command true
directory=$provider_host_root/components/helper
[[ $(stat -c '%u:%g:%a' -- "$directory" "$directory/diagnostics" | sort -u) == "$(id -u):$(id -g):700" ]]
[[ $(stat -c '%u:%g:%a' -- "$directory/posture.stdout" "$directory/mounts.stdout" | sort -u) == "$(id -u):$(id -g):600" ]]
provider_host_remove helper
[[ ${#provider_host_ids[@]} == 0 ]]
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                create = host.commands()[0]
                image_index = create.index(host.environment["FAKE_IMAGE"].encode())
                self.assertEqual(create[create.index(b"--network") + 1], network.encode())
                self.assertIn(b"PYTHONPATH=/fixture", create[:image_index])
                self.assertIn(b"type=bind,src=/private/case file,dst=/input/case.json,readonly", create[:image_index])
                self.assertEqual(
                    create[image_index + 5 :],
                    [
                        interpreter.encode(),
                        b"-B",
                        ("/fixture/" + script).encode(),
                        b"--literal",
                        b"$(touch /tmp/never-execute); literal value",
                    ],
                )
                self.assertEqual(
                    (host.root / "install/components/helper/mounts.stdout").read_text(), host.environment["FAKE_MOUNTS"] + "\n"
                )
                self.assertEqual(
                    [row[0] for row in host.commands()],
                    [
                        b"create",
                        b"inspect",
                        b"inspect",
                        b"start",
                        b"stop",
                        b"inspect",
                        b"logs",
                        b"rm",
                    ],
                )
