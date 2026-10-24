"""The maintained caller delegates each profile with exact grants and clocks.

Only the shell composition runs: finite recording owners replace the engine,
named deployment and helper processes. No API, model or scientific work runs.
"""

from tools.container_guard import require_container

require_container()

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

OWNER = r"""#!/usr/bin/env bash
if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    printf 'obsolete foreground invoked\n' >&2; exit 97
fi
provider_host_path() { [[ $1 == /* ]]; }
provider_host_disjoint() { [[ $1 != "$2" && $1 != "$2/"* ]]; }
provider_host_ipv4() { [[ $1 == 127.0.0.1 || $1 == 192.0.2.53 || $1 == 198.51.100.53 ]]; }
provider_host_refuse() { provider_host_diagnostic=$1; return 2; }
provider_host_initialize() {
    mkdir -p "$provider_host_root/components" "$provider_host_root/routes"
    source "$repository_root/tools/docker_client.sh"
    inference_retirement_confirmed=true
    provider_host_stop_requested=false
    declare -gA provider_host_ids=()
}
repository_git() { printf '%040d\n' 1; }
provider_host_create() {
    local role=$1 network=$2 image=$3 record_mounts=$6
    local -n captured_grants=$4 captured_command=$5
    mkdir -p "$provider_host_root/components/$role"
    printf '%s\n' "$role|$network|$image|$record_mounts" >> "$TEST_RECORDS/creates"
    printf 'create-%s\n' "$role" >> "$TEST_RECORDS/events"
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
        stage)
            printf 'nmr.localhost\n' > "$provider_host_root/source/host.txt"
            mkdir -m 700 -p "$provider_host_root/deployment/secrets"
            for file in provider.toml deployment.toml; do printf '%s\n' "$file" > "$provider_host_root/deployment/$file"; done
            for file in signing.private.json interpreter.key api-ca.crt model-ca.pem; do
                printf 'fixture-%s\n' "$file" > "$provider_host_root/deployment/secrets/$file"
            done
            printf 'unselected\n' > "$provider_host_root/deployment/secrets/extra-canary"
            ;;
        admission)
            printf 'model.example:443\n' > "$provider_host_root/admission/authority.txt"
            printf '2420\n' > "$provider_host_root/admission/host-seconds.txt"
            printf '{}\n' > "$provider_host_root/admission/profile.json"
            ;;
        proxy)
            "$TEST_PYTHON" -c 'import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])' "$provider_host_root/routes/model/proxy.sock"
            printf 'ready\n' > "$provider_host_root/routes/model/ready"
            ;;
        probe)
            if [[ ${TEST_SOURCE_CHANGED-false} == true ]]; then
                printf 'changed\n' >> "$repository_root/tests/fixtures/preparation/interpreter_ambiguity.json"
            fi
            ;;
    esac
}
provider_host_remove() {
    [[ -v provider_host_ids[$1] ]] || return 0
    printf '%s\n' "$1" >> "$TEST_RECORDS/removals"
    printf '%s\n' "$1" >> "$TEST_RECORDS/events"
    unset 'provider_host_ids[$1]'
}
provider_host_command() {
    local operation=$1 directory=$2
    case "$operation" in
        network) printf '%064d|fixture-network|true\n' 2 > "$directory/network.stdout" ;;
        observation)
            local code=0
            if [[ ${directory##*/} == probe && ${TEST_PROBE_FAILED-false} == true ]]; then code=1; fi
            printf '%064d|exited|false|%s\n' 1 "$code" > "$directory/observation.stdout"
            printf '%s|%s\n' "${directory##*/}" "$((deadline - SECONDS))" >> "$TEST_RECORDS/waits"
            ;;
        membership) printf '127.0.0.1\n' > "$directory/membership.stdout" ;;
        signal)
            [[ -f $TEST_RECORDS/down-succeeded ]] || return 98
            printf 'signal-peer\n' >> "$TEST_RECORDS/events"
            printf '%s\n' '{"requests": 1, "handlers_settled": true}' > "$provider_host_root/peer/retired.json"
            ;;
    esac
}
"""

DEPLOYMENT = r'''#!/usr/bin/env bash
set -eu
[[ $# == 1 && $1 =~ ^(install|up|down)$ ]]
[[ $NMRNET_DEPLOYMENT_INPUT =~ ^qualification-api-[A-Za-z0-9]{8}$ ]]
printf '%s|%s\n' "$1" "$NMRNET_DEPLOYMENT_INPUT" >> "$TEST_RECORDS/deployments"
printf '%s\n' "$1" >> "$TEST_RECORDS/events"
root=$(cd -- "$(dirname -- "$0")/.." && pwd -P)
public=$root/config/deployments/$NMRNET_DEPLOYMENT_INPUT
private=$root/secrets/deployments/$NMRNET_DEPLOYMENT_INPUT
for file in provider.toml deployment.toml; do cmp -s "$public/$file" <(printf '%s\n' "$file"); done
for file in signing.private.json interpreter.key api-ca.crt model-ca.pem; do
    cmp -s "$private/$file" <(printf 'fixture-%s\n' "$file")
    [[ $(stat -c '%a' "$private/$file") == 600 ]]
done
[[ $(stat -c '%a' "$private") == 700 && ! -e $private/extra-canary ]]
if [[ ${TEST_FAIL-} == "$1" ]]; then exit 7; fi
if [[ $1 == down ]]; then touch "$TEST_RECORDS/down-succeeded"; fi
'''


class ApiQualificationCompositionTests(unittest.TestCase):
    def _ambiguity(self, root, *, missing=None, failed=False, changed=False, probe="ambiguity"):
        repository, records, output = root / "repository", root / "records", root / "output"
        records.mkdir()
        script = repository / "tools/provider_api_qualification.sh"
        script.parent.mkdir(parents=True)
        shutil.copyfile("/fixture/provider_api_qualification.sh", script)
        (repository / "tools/provider.sh").write_text(OWNER)
        (repository / "tools/docker_client.sh").write_text("docker_client_cleanup() { :; }\n")
        image = "sha256:" + "a" * 64
        (repository / "tools/provider_image.sh").write_text(f"require_provider_image() {{ provider_image_id={image}; }}\n")
        for name in (
            "Makefile",
            "tools/inference_native.sh",
            "tools/provider_logging.sh",
            "tools/worker_retirement.sh",
            "tools/git_metadata.sh",
            "tests/integration/provider_runtime/interpreter_ambiguity.py",
            "tests/integration/provider_runtime/stage_api_config.py",
            "tests/integration/provider_core/config_ports.py",
            "packages/provider-core/tests/__init__.py",
            "packages/provider-core/tests/fakes/__init__.py",
            "packages/provider-core/tests/fakes/credential.py",
            "tests/fixtures/preparation/interpreter_ambiguity.json",
            "tests/fixtures/preparation/interpreter_formats.json",
        ):
            target = repository / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        environment = {
            "PATH": os.environ["PATH"],
            "TEST_RECORDS": str(records),
            "TEST_KEY": str(root / "model.key"),
            "TEST_CA": str(root / "model-ca.pem"),
            "TEST_PYTHON": sys.executable,
            "TEST_PROBE_FAILED": str(failed).lower(),
            "TEST_SOURCE_CHANGED": str(changed).lower(),
            "NMRNET_INTERPRETER_CONFIG_INPUT": str(root / "interpreter.toml"),
            "NMRNET_EXTERNAL_INTERFACE_INPUT": "fixture0",
            "NMRNET_RESOLVERS_INPUT": "192.0.2.53 198.51.100.53",
            "NMRNET_OUTPUT_INPUT": str(output),
        }
        if missing is not None:
            del environment["NMRNET_" + missing + "_INPUT"]
        result = subprocess.run(
            ["bash", str(script), "real-" + probe], env=environment, capture_output=True, text=True, timeout=10, check=False
        )
        return result, records, output, root / "model.key", root / "model-ca.pem"

    def test_ambiguity_mode_has_one_external_route_and_no_api_native_or_state_grants(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, records, output, key, ca = self._ambiguity(Path(temporary))
            self.assertEqual(result.returncode, 0, result.stderr)
            creates = [line.split("|") for line in (records / "creates").read_text().splitlines()]
            self.assertEqual(
                [(row[0], row[1]) for row in creates],
                [("selection", "none"), ("admission", "none"), ("proxy", "host"), ("probe", "none")],
            )
            self.assertTrue(all(row[3] == "true" for row in creates))
            selection = (records / "selection.grants").read_text()
            proxy = (records / "proxy.grants").read_text()
            probe = (records / "probe.grants").read_text()
            self.assertNotIn(str(key), selection + proxy)
            self.assertNotIn(str(ca), selection + proxy)
            self.assertIn(f"src={key},dst=/input/interpreter.key,readonly", probe)
            self.assertIn(f"src={ca},dst=/input/interpreter-ca.pem,readonly", probe)
            for forbidden in (
                "provider.json",
                "registration.json",
                "signing.private.json",
                "prediction-models",
                "/native/",
                "store_root",
            ):
                self.assertNotIn(forbidden, selection + proxy + probe)
            argv = (records / "proxy.argv").read_text().split("\0")[:-1]
            self.assertEqual(
                argv[-12:],
                [
                    "--mode",
                    "external",
                    "--authority",
                    "model.example:443",
                    "--interface",
                    "fixture0",
                    "--socket",
                    "/route/proxy.sock",
                    "--ready-file",
                    "/route/ready",
                    "--status-directory",
                    "/diagnostics",
                ],
            )
            self.assertEqual((output / "resolv.conf").read_text(), "nameserver 192.0.2.53\nnameserver 198.51.100.53\n")
            waits = dict(line.split("|", 1) for line in (records / "waits").read_text().splitlines())
            self.assertIn(int(waits["probe"]), (2420, 2419))
            self.assertEqual((records / "removals").read_text().splitlines(), ["selection", "admission", "probe", "proxy"])
            self.assertIn("no API Jobs or science", result.stdout)
            self.assertFalse((records / "deployments").exists())

    def test_format_choice_mode_reuses_the_same_route_retirement_and_metadata_only_grants(self):
        with tempfile.TemporaryDirectory() as temporary:
            result, records, _output, _key, _ca = self._ambiguity(Path(temporary), probe="formats")
            self.assertEqual(result.returncode, 0, result.stderr)
            creates = [line.split("|")[:2] for line in (records / "creates").read_text().splitlines()]
            self.assertEqual(creates, [["selection", "none"], ["admission", "none"], ["proxy", "host"], ["probe", "none"]])
            for role in ("admission", "probe"):
                argv = (records / (role + ".argv")).read_text().split("\0")
                self.assertEqual(argv[argv.index("--probe") + 1], "formats")
            grants = (records / "probe.grants").read_text()
            self.assertIn("interpreter_formats.json,dst=/fixture/cases.json,readonly", grants)
            self.assertNotIn("/native", grants)
            self.assertNotIn("/input/provider.json", grants)
            self.assertNotIn("/input/retained-result.json", grants)
            self.assertEqual((records / "removals").read_text().splitlines(), ["selection", "admission", "probe", "proxy"])
            self.assertFalse((records / "deployments").exists())

    def test_ambiguity_selector_refusal_has_zero_helper_dispatch(self):
        for missing in ("INTERPRETER_CONFIG", "EXTERNAL_INTERFACE", "RESOLVERS", "OUTPUT"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                result, records, _output, _key, _ca = self._ambiguity(Path(temporary), missing=missing)
                self.assertEqual(result.returncode, 2)
                self.assertIn(missing, result.stderr)
                self.assertFalse((records / "creates").exists())

    def test_ambiguity_failure_or_changed_sources_still_retires_original_helpers(self):
        for failed, changed in ((True, False), (False, True)):
            with self.subTest(failed=failed, changed=changed), tempfile.TemporaryDirectory() as temporary:
                result, records, _output, _key, _ca = self._ambiguity(Path(temporary), failed=failed, changed=changed)
                self.assertEqual(result.returncode, 2)
                self.assertNotIn("qualified;", result.stdout)
                removals = (records / "removals").read_text().splitlines()
                self.assertLess(removals.index("probe"), removals.index("proxy"))
                self.assertEqual((records / "creates").read_text().count("probe|"), 1)
                if changed:
                    self.assertIn("source bytes changed", result.stderr)

    def test_actual_script_delegates_scripted_and_real_profiles_without_cross_grants(self):
        deployment_names = set()
        for profile, clocks in (
            ("scripted-pasted", (180, 240, 260)),
            ("real-pasted", (900, 960, 980)),
            ("real-smiles", (900, 960, 980)),
            ("real-smiles-multiple", (900, 960, 980)),
            ("real-upload", (900, 1020, 1040)),
            ("real-sdf-zip", (900, 1200, 1220)),
            ("resume-sdf-zip", (900, 1200, 1220)),
            ("failed-install", (180, 240, 260)),
            ("failed-up", (180, 240, 260)),
            ("failed-down", (180, 240, 260)),
        ):
            with self.subTest(mode=profile), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repository, records, output = root / "repository", root / "records", root / "output"
                records.mkdir()
                resumed = profile == "resume-sdf-zip"
                failed = profile.removeprefix("failed-") if profile.startswith("failed-") else None
                mode = "scripted-pasted" if failed else "real-sdf-zip" if resumed else profile
                script = repository / "tools/provider_api_qualification.sh"
                script.parent.mkdir(parents=True)
                shutil.copyfile("/fixture/provider_api_qualification.sh", script)
                (repository / "tools/provider.sh").write_text(OWNER)
                (repository / "tools/provider_deployment.sh").write_text(DEPLOYMENT)
                shutil.copyfile("/fixture/operator_guidance.sh", repository / "tools/operator_guidance.sh")
                (repository / "config/deployments").mkdir(parents=True)
                (repository / "tools/docker_client.sh").write_text(
                    "docker_client_prepare() { :; }\ndocker_client_cleanup() { :; }\n"
                )
                for name in (
                    "Makefile",
                    "config/provider.toml.example",
                    "tools/inference_native.sh",
                    "tools/worker_retirement.sh",
                    "tools/deployment_helper_grants.sh",
                    "tools/deployment_runtime_recovery.sh",
                    "tools/provider_deployment.py",
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
                    "tools/provider_image.sh",
                    "tools/provider_logging.sh",
                    "tools/bound_http_proxy.py",
                    "tools/provider_proxy.py",
                    "tools/container_guard.py",
                    "packages/provider-core/tests/__init__.py",
                    "packages/provider-core/tests/fakes/__init__.py",
                    "tests/fixtures/preparation/api_sdf_zip.json",
                    "tests/fixtures/preparation/normative/butane_conformers.sdf",
                ):
                    target = repository / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.touch()
                image = "sha256:" + "a" * 64
                image_record = repository / ".work/provider/image-id"
                image_record.parent.mkdir(parents=True)
                image_record.write_text(image + "\n")
                if mode == "real-sdf-zip":
                    # The Git capture owner has separate tests; here record its
                    # closed input grant and copy authored fixture bytes only.
                    (repository / "tools/git_metadata.sh").write_text(
                        'repository_git() { if [[ $* != *status* ]]; then printf "%040d\\n" 1; fi; }\n'
                    )
                    (repository / "tools/git_inputs.sh").write_text(
                        "copy_git_blobs() { local source=$2 target=$4; shift 5; "
                        'printf "%s\\n" "$@" > "$TEST_RECORDS/capture.paths"; '
                        'for path in "$@"; do mkdir -p "$(dirname -- "$target/$path")"; cp -- "$source/$path" "$target/$path"; done; }\n'
                    )
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
                if resumed:
                    environment["NMRNET_UPLOAD_RECEIPT_INPUT"] = str(root / "original-upload.json")
                if failed:
                    environment["TEST_FAIL"] = failed
                result = subprocess.run(
                    ["bash", str(script), mode], env=environment, capture_output=True, text=True, timeout=10, check=False
                )
                calls = [row.split("|") for row in (records / "deployments").read_text().splitlines()]
                if failed:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("qualified;", result.stdout)
                    if failed == "down":
                        self.assertEqual([row[0] for row in calls], ["install", "up", "down", "down"])
                        self.assertEqual((records / "creates").read_text().count("client|"), 1)
                        events = (records / "events").read_text().splitlines()
                        self.assertNotIn("signal-peer", events)
                        self.assertNotIn("peer", events)
                        self.assertIn("provider/deployment/down", result.stderr)
                        self.assertIn("DEPLOYMENT=" + calls[0][1], result.stderr)
                    else:
                        self.assertNotIn("client|", (records / "creates").read_text())
                        self.assertEqual([row[0] for row in calls], ["install", "up", "down"] if failed == "up" else ["install"])
                    if failed == "up":
                        events = (records / "events").read_text().splitlines()
                        self.assertLess(events.index("down"), events.index("peer"))
                    continue
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([row[0] for row in calls], ["install", "up", "down"])
                self.assertEqual({row[1] for row in calls}, {(output / "deployment-name").read_text().strip()})
                self.assertNotIn(calls[0][1], deployment_names)
                deployment_names.add(calls[0][1])
                events = (records / "events").read_text().splitlines()
                self.assertLess(events.index("up"), events.index("create-client"))
                self.assertLess(events.index("client"), events.index("down"))
                if mode == "scripted-pasted":
                    self.assertLess(events.index("down"), events.index("signal-peer"))
                    self.assertLess(events.index("signal-peer"), events.index("peer"))
                stage_argv = (records / "stage.argv").read_text().split("\0")[:-1]
                self.assertIn("--deployment", stage_argv)
                self.assertEqual(stage_argv[stage_argv.index("--api-interface") + 1], "lo")
                self.assertEqual(stage_argv[stage_argv.index("--api-address") + 1], "127.0.0.1")
                client_argv = (records / "client.argv").read_text().split("\0")[:-1]
                self.assertEqual(
                    client_argv[-8:-2] if resumed else client_argv[-6:],
                    [
                        "--readback-seconds",
                        str(clocks[0]),
                        "--process-seconds",
                        str(clocks[1]),
                        "--case-kind",
                        mode.removeprefix("real-") if mode != "scripted-pasted" else "pasted",
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
                self.assertIn(f"type=bind,src={output}/deployment,dst={output}/deployment", stage_grants)
                self.assertFalse(any(grant.startswith(f"type=bind,src={output},") for grant in stage_grants))
                client_grants = (records / "client.grants").read_text().split("\0")[:-1]
                self.assertEqual(any("dst=/input/upload-receipt.json,readonly" in grant for grant in client_grants), resumed)
                if resumed:
                    self.assertEqual(client_argv[-2:], ["--upload-receipt", "/input/upload-receipt.json"])
                self.assertTrue(any("src=" + selectors["PROVIDER_CREDENTIAL"] + "," in grant for grant in stage_grants))
                self.assertFalse(any("src=" + selectors["PROVIDER_CREDENTIAL"] + "," in grant for grant in client_grants))
                self.assertFalse(any(selected_key in grant for grant in client_grants))
                expected_fixture = (
                    "api_sdf_zip.json"
                    if mode == "real-sdf-zip"
                    else "api_smiles_multiple.json"
                    if mode == "real-smiles-multiple"
                    else "api_" + mode.rsplit("-", 1)[-1] + ".json"
                )
                source_root = output / "qualification-source" if mode == "real-sdf-zip" else repository
                self.assertTrue(
                    any(
                        f"src={source_root}/tests/fixtures/preparation/{expected_fixture},dst=/fixture/case.json,readonly"
                        in grant
                        for grant in client_grants
                    )
                )
                self.assertIn(
                    str("tests/fixtures/preparation/" + expected_fixture),
                    (output / "qualification-sources.sha256").read_text(),
                )
                self.assertEqual(
                    any("dst=/fixture/smiles.json," in grant for grant in client_grants),
                    mode in ("real-smiles", "real-smiles-multiple"),
                )
                self.assertEqual(any("dst=/fixture/atom_order.xyz," in grant for grant in client_grants), mode == "real-upload")
                self.assertEqual(any("dst=/fixture/expected.json," in grant for grant in client_grants), mode == "real-upload")
                self.assertEqual(
                    any("dst=/fixture/butane_conformers.sdf," in grant for grant in client_grants), mode == "real-sdf-zip"
                )
                if mode == "real-sdf-zip":
                    capture = (records / "capture.paths").read_text().splitlines()
                    self.assertIn("tests/integration/provider_runtime/api_prediction.py", capture)
                    self.assertIn("tests/fixtures/preparation/normative/butane_conformers.sdf", capture)
                if mode != "scripted-pasted":
                    self.assertEqual(stage_argv[stage_argv.index("--interpreter-route") + 1], "external")
                    self.assertEqual(stage_argv[stage_argv.index("--interpreter-interface") + 1], "fixture0")
                    self.assertEqual([stage_argv[index + 1] for index, arg in enumerate(stage_argv) if arg == "--resolver"], selectors["RESOLVERS"].split())
                    self.assertNotIn("--interpreter-network", stage_argv)
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
                    self.assertEqual(stage_argv[stage_argv.index("--interpreter-route") + 1], "local")
                    self.assertEqual(stage_argv[stage_argv.index("--interpreter-interface") + 1], "eth0")
                    self.assertEqual(stage_argv[stage_argv.index("--interpreter-network") + 1], f"{2:064d}")
                    self.assertEqual(stage_argv[stage_argv.index("--interpreter-address") + 1], "127.0.0.1")
                    self.assertNotIn("--resolver", stage_argv)
                    self.assertFalse(any("interpreter.key" in grant for grant in stage_grants))
