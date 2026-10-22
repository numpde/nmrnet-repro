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
    def test_recovery_handoff_admits_all_seven_fields_only_after_the_selected_helper_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff"
            fields = (
                b"/private/source",
                b"provider.toml",
                b"b" * 64,
                b"/private/state",
                b"api.example:443",
                b"/private/source/signing.json",
                b"",
            )
            path.write_bytes(b"\0".join(fields) + b"\0")
            path.chmod(0o600)
            for role in ("metadata", "capture"):
                with self.subTest(role=role):
                    result = subprocess.run(
                        [
                            "bash",
                            "-euc",
                            r"""
source tools/provider.sh
declare -A provider_host_ids=()
provider_host_capture_status=0
provider_host_adopt_plan "$1" 7 "$2"
printf '%s\0' "${provider_host_fields[@]}"
""",
                            "recovery-proof",
                            str(path),
                            role,
                        ],
                        env=os.environ,
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, path.read_bytes())

    def test_recovery_arguments_grant_only_toml_then_exact_api_files_and_never_scientific_inputs(self):
        for mode in ("inspect", "run"):
            for phase in ("metadata", "capture", "controller"):
                with self.subTest(mode=mode, phase=phase):
                    result = subprocess.run(
                        [
                            "bash",
                            "-euc",
                            r"""
source tools/provider.sh
provider_host_recovery_mode=$1
provider_host_root=/private/recovery
NMRNET_CONFIG_INPUT=/private/config/provider.toml
NMRNET_SOURCE_ROOT_INPUT=/private/config
NMRNET_MAXIMUM_STARTUP_BYTES_INPUT=131072
NMRNET_RECORD_SHA256_INPUT=$(printf 'a%.0s' {1..64})
provider_host_fields=(/private/config provider.toml "$(printf 'b%.0s' {1..64})" /private/state '' '' '')
if [[ $1 == run ]]; then provider_host_fields[4]=api.example:443; provider_host_fields[5]=/private/config/signing.json; provider_host_fields[6]=/private/config/api-ca.pem; fi
provider_host_recovery_arguments "$2"
printf '%s\0' "${provider_host_grants[@]}" "${provider_host_argv[@]}"
""",
                            "recovery-proof",
                            mode,
                            phase,
                        ],
                        env=os.environ,
                        capture_output=True,
                        timeout=5,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    values = result.stdout.split(b"\0")[:-1]
                    mounts = [value for value in values if value.startswith(b"type=bind,")]
                    if phase == "metadata":
                        self.assertEqual(
                            mounts, [b"type=bind,src=/private/config/provider.toml,dst=/private/config/provider.toml,readonly"]
                        )
                    elif phase == "capture":
                        self.assertIn(b"type=bind,src=/private/recovery/output,dst=/output", mounts)
                        self.assertEqual(len(mounts), 4 if mode == "run" else 2)
                    else:
                        self.assertIn(
                            b"type=bind,src=/private/recovery/output/snapshot/inputs,dst=/private/config,readonly", mounts
                        )
                        self.assertIn(b"type=bind,src=/private/state,dst=/private/state", mounts)
                        self.assertEqual(len(mounts), 3 if mode == "run" else 2)
                        if mode == "run":
                            self.assertIn(b"--record-sha256", values)
                            self.assertIn(b"--api-proxy-socket", values)
                        else:
                            self.assertNotIn(b"--api-proxy-socket", values)
                    if mode == "inspect" or phase == "metadata":
                        self.assertNotIn(b"signing.json", result.stdout)
                        self.assertNotIn(b"api-ca.pem", result.stdout)
                    for forbidden in (
                        b"interpreter.key",
                        b"/native",
                        b"prediction-models",
                        b"execution.json",
                        b"/routes/interpreter",
                        b"/routes/store",
                    ):
                        self.assertNotIn(forbidden, result.stdout)

    def test_recovery_drain_uses_no_native_pump_and_retires_controller_before_its_only_api_proxy(self):
        for mode, controller_exit in (("inspect", "0"), ("run", "0"), ("run", "7")):
            with self.subTest(mode=mode, controller_exit=controller_exit):
                host = HostFixture()
                self.addCleanup(host.close)
                host.environment.update(FAKE_RECOVERY_MODE=mode, FAKE_CONTROLLER_EXIT=controller_exit)
                result = host.run(r"""
source tools/provider.sh
source tools/docker_client.sh
umask 077
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components" "$provider_host_root/components/controller"
printf '%s\n' "$FAKE_CID" > "$provider_host_root/components/controller/worker.cid"
declare -A provider_host_ids=([controller]="$FAKE_CID")
if [[ $FAKE_RECOVERY_MODE == run ]]; then mkdir -m 700 "$provider_host_root/components/api"; printf '%s\n' "$FAKE_CID" > "$provider_host_root/components/api/worker.cid"; provider_host_ids[api]=$FAKE_CID; fi
provider_host_recovery_mode=$FAKE_RECOVERY_MODE
provider_host_diagnostic= provider_host_drain_seconds=2 provider_host_controller_running=true provider_host_controller_exit=0
provider_host_native_open=false provider_host_term_attempted=false provider_host_stop_requested=true docker_client_directory=
provider_workers_diagnostic=
provider_workers_service() { printf 'forbidden native pump\n' > "$FAKE_STATE/native-pump"; return 97; }
exec {provider_host_lock}< "$provider_host_root"
provider_host_finish 0
""")
                self.assertEqual(result.returncode, 0 if controller_exit == "0" else 2, result.stderr)
                self.assertFalse((host.root / "native-pump").exists())
                if controller_exit == "0":
                    self.assertEqual(
                        result.stdout,
                        f"Protected recovery result: {host.root}/install/components/controller/diagnostics/result.json\n",
                    )
                else:
                    self.assertNotIn("Protected recovery result", result.stdout)
                self.assertEqual(
                    [row[0] for row in host.commands()],
                    [b"kill", b"inspect"] + [b"stop", b"inspect", b"rm"] * (2 if mode == "run" else 1),
                )

    def test_signal_during_liveness_preserves_the_poll_and_drains_from_a_fresh_controller_observation(self):
        for role in ("controller", "store"):
            with self.subTest(role=role):
                host = HostFixture()
                self.addCleanup(host.close)
                host.environment.update(
                    FAKE_OBSERVED_ROLE=role,
                    FAKE_OBSERVATION_SIGNAL_ONCE="yes",
                    FAKE_OBSERVATION_SIGNAL_AT="1" if role == "controller" else "4",
                    FAKE_CONTROLLER_EXIT="0",
                )
                result = host.run(r"""
source tools/provider.sh
source tools/docker_client.sh
umask 077
provider_host_root=$FAKE_STATE/install
mkdir -m 700 -- "$provider_host_root" "$provider_host_root/components"
for role in controller api interpreter store; do
    mkdir -m 700 -- "$provider_host_root/components/$role"
    printf '%s\n' "$FAKE_CID" > "$provider_host_root/components/$role/worker.cid"
done
declare -A provider_host_ids=([controller]="$FAKE_CID" [api]="$FAKE_CID" [interpreter]="$FAKE_CID" [store]="$FAKE_CID")
provider_host_diagnostic= provider_host_drain_seconds=2 provider_host_controller_running=true provider_host_controller_exit=0
provider_host_native_open=false provider_host_term_attempted=false provider_host_stop_requested=false docker_client_directory=
provider_workers_admission=open
provider_workers_service() { :; }
exec {provider_host_lock}< "$provider_host_root"
trap 'provider_host_stop_requested=true' HUP INT TERM
export FAKE_OWNER_PID=$BASHPID
provider_host_monitor
[[ $provider_host_stop_requested == true && -z $provider_host_diagnostic ]]
[[ $inference_retirement_confirmed == true && $inference_cancellation_status == 143 ]]
[[ $provider_host_controller_running == true ]]
directory=$provider_host_root/components/$FAKE_OBSERVED_ROLE
cmp -s -- "$directory/interrupted-observation.stdout" <(printf '%s|running|true|0\n' "$FAKE_CID")
[[ ! -e $directory/observation.stdout && ! -e $directory/observation.stderr ]]
cmp -s -- "$directory/interrupted-observation.status" <(printf 'native_status=143\ncancellation=143\nlocal_observer_retired=true\n')
provider_host_finish 0
""")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    [row[0] for row in host.commands()],
                    [b"inspect"] * (1 if role == "controller" else 4) + [b"kill", b"inspect"] + [b"stop", b"inspect", b"rm"] * 4,
                )
                controller = host.root / "install/components/controller"
                self.assertEqual(
                    (controller / "observation.stdout").read_text(), host.environment["FAKE_CID"] + "|exited|false|0\n"
                )
                self.assertTrue((host.root / f"install/components/{role}/interrupted-observation.stderr").is_file())

    def test_mapped_api_and_store_keep_loopback_while_external_model_uses_its_own_interface(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    "bash",
                    "-euc",
                    r"""
source tools/provider.sh
provider_host_root=$1 provider_host_diagnostic=
provider_host_local_peers= provider_host_local_network= provider_host_local_interface=
provider_host_modes=(mapped external mapped)
provider_host_fields=('' '' '' 'nmr.localhost:10443' 'api.openai.com:443' 'nmr.localhost:10443')
provider_host_mapped_addresses=nmr.localhost=127.0.0.1
provider_host_mapped_interface=lo provider_host_external_interface=wlp1s0
provider_host_resolvers=192.0.2.53
provider_host_command() { exit 97; }
provider_host_routes
index=0
for role in api interpreter store; do
    provider_host_proxy_arguments "$role" "$index"
    printf '%s\0' "$role" "$provider_host_proxy_network" "${provider_host_proxy_args[@]}" "${provider_host_grants[@]}"
    printf '\0'
    index=$((index + 1))
done
""",
                    "mixed-route-proof",
                    directory,
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            chunks = result.stdout.split(b"\0\0")
            self.assertEqual(chunks[-1], b"")
            routes = {fields[0]: fields[1:] for fields in (chunk.split(b"\0") for chunk in chunks[:-1])}
            self.assertEqual(set(routes), {b"api", b"interpreter", b"store"})
            for role in (b"api", b"store"):
                arguments = routes[role]
                self.assertEqual(arguments[0], b"host")
                self.assertEqual(arguments[arguments.index(b"--interface") + 1], b"lo")
                self.assertIn(b"nmr.localhost:10443", arguments)
                self.assertIn(b"nmr.localhost=127.0.0.1", arguments)
                self.assertFalse(any(b"resolv.conf" in argument for argument in arguments))
            model = routes[b"interpreter"]
            self.assertEqual(model[0], b"host")
            self.assertEqual(model[model.index(b"--interface") + 1], b"wlp1s0")
            self.assertIn(b"api.openai.com:443", model)
            self.assertNotIn(b"--address", model)
            self.assertIn(f"type=bind,src={directory}/resolv.conf,dst=/etc/resolv.conf,readonly".encode(), model)
            self.assertEqual((Path(directory) / "resolv.conf").read_bytes(), b"nameserver 192.0.2.53\n")

    def test_mapped_host_publication_needs_no_dns_or_engine_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    "bash",
                    "-euc",
                    r"""
source tools/provider.sh
provider_host_root=$1 provider_host_diagnostic=
provider_host_local_peers= provider_host_local_network= provider_host_local_interface= provider_host_resolvers=
provider_host_modes=(mapped mapped mapped)
provider_host_fields=('' '' '' 'nmr.localhost:10443' 'model.localhost:9443' 'nmr.localhost:10443')
provider_host_mapped_addresses='nmr.localhost=172.17.0.1 model.localhost=127.0.0.1'
provider_host_mapped_interface=lo provider_host_external_interface=
provider_host_command() { exit 97; }
provider_host_routes
[[ ${provider_host_addresses[mapped:nmr.localhost]} == 172.17.0.1 ]]
[[ ${provider_host_addresses[mapped:model.localhost]} == 127.0.0.1 ]]
[[ ! -e $provider_host_root/resolv.conf ]]
provider_host_fields[4]=$'model.localhost:9443\nmodel.localhost:9444'
provider_host_proxy_arguments interpreter 1
[[ $provider_host_proxy_network == host ]]
printf '%s\0' "${provider_host_proxy_args[@]}"
""",
                    "mapped-proof",
                    directory,
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.count(b"model.localhost=127.0.0.1\0"), 1)
            self.assertIn(b"model.localhost:9443\0", result.stdout)
            self.assertIn(b"model.localhost:9444\0", result.stdout)

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
provider_host_argv=(/opt/provider/environment/bin/python -I -B -m nmrnet_provider.launch_capture --maximum-bytes "$maximum")
provider_host_create capture none "$controller_image_id" provider_host_grants provider_host_argv false
[[ ! -e $provider_host_root/components/capture/mounts.stdout ]]
provider_host_remove capture
[[ ${#provider_host_ids[@]} == 0 ]]
""")
        self.assertEqual(result.returncode, 0, result.stderr)
        create = host.commands()[0]
        self.assertEqual(create[create.index(b"--maximum-bytes") + 1], b"2097152")
        self.assertEqual(
            create[create.index(host.environment["FAKE_IMAGE"].encode()) + 5 :],
            [
                b"/opt/provider/environment/bin/python",
                b"-I",
                b"-B",
                b"-m",
                b"nmrnet_provider.launch_capture",
                b"--maximum-bytes",
                b"2097152",
            ],
        )
        self.assertNotIn(b"PYTHONPATH=/fixture", create)
        self.assertEqual([row[0] for row in host.commands()], [b"create", b"inspect", b"start", b"stop", b"inspect", b"rm"])

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
                        b"rm",
                    ],
                )

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
