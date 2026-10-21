"""The maintained caller delegates each profile with exact grants and clocks.

Only the shell composition runs: finite recording owners replace the engine,
foreground service and helper processes. No API, model or scientific work runs.
"""

from tools.container_guard import require_container

require_container()

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

OWNER = r"""#!/usr/bin/env bash
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    env -0 > "$TEST_RECORDS/foreground.env"
    mkdir -p "$NMRNET_INSTALLATION_ROOT_INPUT/components/controller/diagnostics"
    touch "$NMRNET_INSTALLATION_ROOT_INPUT/components/controller/diagnostics/admitted"
    trap 'exit 0' TERM
    while :; do sleep 0.05; done
fi
provider_host_path() { [[ $1 == /* ]]; }
provider_host_disjoint() { [[ $1 != "$2" && $1 != "$2/"* ]]; }
provider_host_ipv4() { [[ $1 == 127.0.0.1 ]]; }
provider_host_refuse() { provider_host_diagnostic=$1; return 2; }
provider_host_create() {
    local role=$1 network=$2 image=$3 record_mounts=$6
    local -n captured_grants=$4 captured_command=$5
    mkdir -p "$provider_host_root/components/$role"
    printf '%s\n' "$role|$network|$image|$record_mounts" >> "$TEST_RECORDS/creates"
    printf '%s\0' "${captured_grants[@]}" > "$TEST_RECORDS/$role.grants"
    printf '%s\0' "${captured_command[@]}" > "$TEST_RECORDS/$role.argv"
    provider_host_ids[$role]=$(printf '%064d' 1)
    case "$role" in
        peer) printf '{}\n' > "$provider_host_root/peer/ready.json" ;;
        selection)
            printf '%s\n' "$TEST_KEY" > "$provider_host_root/selection/key_file.txt"
            printf '%s\n' "$TEST_CA" > "$provider_host_root/selection/ca_file.txt"
            printf '{}\n' > "$provider_host_root/selection/interpreter-inputs.json"
            ;;
        stage) printf 'nmr.localhost\n' > "$provider_host_root/source/host.txt" ;;
    esac
}
provider_host_remove() { unset 'provider_host_ids[$1]'; }
provider_host_command() {
    local operation=$1 directory=$2
    case "$operation" in
        network) printf '%064d|fixture-network|true\n' 2 > "$directory/network.stdout" ;;
        observation)
            printf '%064d|exited|false|0\n' 1 > "$directory/observation.stdout"
            printf '%s|%s\n' "${directory##*/}" "$((deadline - SECONDS))" >> "$TEST_RECORDS/waits"
            ;;
        signal) printf '%s\n' '{"requests": 1, "handlers_settled": true}' > "$provider_host_root/peer/retired.json" ;;
    esac
}
"""


class ApiQualificationCompositionTests(unittest.TestCase):
    def test_actual_script_delegates_scripted_and_real_profiles_without_cross_grants(self):
        for mode, clocks in (
            ("scripted-pasted", (180, 240, 260)),
            ("real-pasted", (900, 960, 980)),
            ("real-smiles", (900, 960, 980)),
            ("real-smiles-multiple", (900, 960, 980)),
            ("real-upload", (900, 1020, 1040)),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repository, records, output = root / "repository", root / "records", root / "output"
                records.mkdir()
                script = repository / "tools/provider_api_qualification.sh"
                script.parent.mkdir(parents=True)
                shutil.copyfile("/fixture/provider_api_qualification.sh", script)
                (repository / "tools/provider.sh").write_text(OWNER)
                (repository / "tools/docker_client.sh").write_text(
                    "docker_client_prepare() { :; }\ndocker_client_cleanup() { :; }\n"
                )
                for name in (
                    "Makefile",
                    "tools/inference_native.sh",
                    "tools/worker_retirement.sh",
                    "tests/integration/provider_runtime/model_peer.py",
                    "tests/integration/provider_runtime/stage_api_config.py",
                    "tests/integration/provider_runtime/api_prediction.py",
                    "tests/integration/provider_core/config_ports.py",
                    "packages/provider-core/tests/fakes/tls_peer.py",
                    "packages/provider-core/tests/fakes/conversation.py",
                    "packages/provider-core/tests/fakes/credential.py",
                    "tests/fixtures/preparation/api_pasted.json",
                    "tests/fixtures/preparation/api_smiles.json",
                    "tests/fixtures/preparation/api_smiles_multiple.json",
                    "tests/fixtures/preparation/api_upload.json",
                    "tests/fixtures/preparation/smiles.json",
                    "tests/fixtures/preparation/expected.json",
                    "tests/fixtures/preparation/normative/atom_order.xyz",
                ):
                    target = repository / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.touch()
                image = "sha256:" + "a" * 64
                image_record = repository / ".work/provider/image-id"
                image_record.parent.mkdir(parents=True)
                image_record.write_text(image + "\n")
                selectors = {
                    "API_ORIGIN": "https://nmr.localhost:10443",
                    "TOPOLOGY": "dev-local",
                    "PROJECT_REF": "project:user-demo",
                    "USER_ID": "demo",
                    "PROVIDER_CREDENTIAL": str(root / "provider.json"),
                    "CLIENT_CREDENTIAL_DIRECTORY": str(root / "client"),
                    "CA_FILE": str(root / "api-ca.pem"),
                    "ADDRESS": "127.0.0.1",
                    "INTERFACE": "lo",
                    "OUTPUT": str(output),
                    "API_CLIENT_IMAGE": image,
                    "RETAINED_RESULT": str(root / "retained.json"),
                    "LOCAL_NETWORK": "fixture-network",
                    "LOCAL_INTERFACE": "eth0",
                    "INTERPRETER_CONFIG": str(root / "selected.toml"),
                    "EXTERNAL_INTERFACE": "fixture0",
                    "RESOLVERS": "192.0.2.53 198.51.100.53",
                }
                selected_key, selected_ca = str(root / "selected-key"), str(root / "selected-ca")
                environment = {
                    "PATH": os.environ["PATH"],
                    "TEST_RECORDS": str(records),
                    "TEST_KEY": selected_key,
                    "TEST_CA": selected_ca,
                }
                environment.update({"NMRNET_" + name + "_INPUT": value for name, value in selectors.items()})
                result = subprocess.run(
                    ["bash", str(script), mode], env=environment, capture_output=True, text=True, timeout=10, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                foreground = dict(item.split("=", 1) for item in (records / "foreground.env").read_text().split("\0") if item)
                self.assertEqual(foreground["NMRNET_MAPPED_INTERFACE_INPUT"], "lo")
                self.assertEqual(foreground["NMRNET_MAPPED_ADDRESSES_INPUT"], "nmr.localhost=127.0.0.1")
                self.assertEqual(foreground["NMRNET_HOST_DRAIN_SECONDS_INPUT"], "90")
                client_argv = (records / "client.argv").read_text().split("\0")[:-1]
                self.assertEqual(
                    client_argv[-6:],
                    [
                        "--readback-seconds",
                        str(clocks[0]),
                        "--process-seconds",
                        str(clocks[1]),
                        "--case-kind",
                        "smiles-multiple" if mode == "real-smiles-multiple" else mode.rsplit("-", 1)[-1],
                    ],
                )
                self.assertEqual(
                    (output / "qualification-profile.txt").read_text(),
                    f"mode={mode}\nreadback_seconds={clocks[0]}\nprocess_seconds={clocks[1]}\nhost_seconds={clocks[2]}\n",
                )
                creates = (records / "creates").read_text().splitlines()
                self.assertEqual(
                    [line.split("|")[0] for line in creates],
                    ["peer", "stage", "client"] if mode == "scripted-pasted" else ["selection", "stage", "client"],
                )
                self.assertTrue(all(line.endswith("|true") for line in creates))
                self.assertIn(f"stage|none|{image}|true", creates)
                self.assertIn(f"client|host|{image}|true", creates)
                waits = dict(line.split("|", 1) for line in (records / "waits").read_text().splitlines())
                self.assertIn(int(waits["client"]), (clocks[2], clocks[2] - 1))
                stage_grants = (records / "stage.grants").read_text().split("\0")[:-1]
                client_grants = (records / "client.grants").read_text().split("\0")[:-1]
                self.assertTrue(any("src=" + selectors["PROVIDER_CREDENTIAL"] + "," in grant for grant in stage_grants))
                self.assertFalse(any("src=" + selectors["PROVIDER_CREDENTIAL"] + "," in grant for grant in client_grants))
                self.assertFalse(any(selected_key in grant for grant in client_grants))
                expected_fixture = (
                    "api_smiles_multiple.json" if mode == "real-smiles-multiple" else "api_" + mode.rsplit("-", 1)[-1] + ".json"
                )
                self.assertTrue(
                    any(
                        f"src={repository}/tests/fixtures/preparation/{expected_fixture},dst=/fixture/case.json,readonly" in grant
                        for grant in client_grants
                    )
                )
                self.assertIn(
                    str(repository / "tests/fixtures/preparation" / expected_fixture),
                    (output / "qualification-sources.sha256").read_text(),
                )
                self.assertEqual(
                    any("dst=/fixture/smiles.json," in grant for grant in client_grants),
                    mode in ("real-smiles", "real-smiles-multiple"),
                )
                self.assertEqual(any("dst=/fixture/atom_order.xyz," in grant for grant in client_grants), mode == "real-upload")
                self.assertEqual(any("dst=/fixture/expected.json," in grant for grant in client_grants), mode == "real-upload")
                if mode != "scripted-pasted":
                    self.assertEqual(foreground["NMRNET_INTERPRETER_ROUTE_INPUT"], "external")
                    self.assertEqual(foreground["NMRNET_EXTERNAL_INTERFACE_INPUT"], "fixture0")
                    self.assertEqual(foreground["NMRNET_RESOLVERS_INPUT"], selectors["RESOLVERS"])
                    for name in ("LOCAL_NETWORK", "LOCAL_INTERFACE", "LOCAL_PEERS"):
                        self.assertEqual(foreground["NMRNET_" + name + "_INPUT"], "")
                    selection_grants = (records / "selection.grants").read_text().split("\0")[:-1]
                    self.assertFalse(any(selected_key in grant or selected_ca in grant for grant in selection_grants))
                    self.assertIn(f"type=bind,src={selected_key},dst=/input/interpreter.key,readonly", stage_grants)
                    self.assertIn(f"type=bind,src={selected_ca},dst=/input/interpreter-ca.pem,readonly", stage_grants)
                    self.assertEqual(
                        (records / "stage.argv").read_text().split("\0")[-3:-1],
                        ["--interpreter-config", "/input/interpreter.toml"],
                    )
                    self.assertFalse(any("model-ready" in grant for grant in stage_grants))
                else:
                    self.assertEqual(foreground["NMRNET_INTERPRETER_ROUTE_INPUT"], "local")
                    self.assertEqual(foreground["NMRNET_LOCAL_INTERFACE_INPUT"], "eth0")
                    self.assertEqual(foreground["NMRNET_EXTERNAL_INTERFACE_INPUT"], "")
                    self.assertEqual(foreground["NMRNET_RESOLVERS_INPUT"], "")
                    self.assertFalse(any("interpreter.key" in grant for grant in stage_grants))
