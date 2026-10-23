"""The maintained caller delegates each profile with exact grants and clocks.

Only the shell composition runs: finite recording owners replace the engine,
foreground service and helper processes. No API, model or scientific work runs.
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
    env -0 > "$TEST_RECORDS/foreground.env"
    mkdir -p "$NMRNET_INSTALLATION_ROOT_INPUT/components/controller/diagnostics"
    touch "$NMRNET_INSTALLATION_ROOT_INPUT/components/controller/diagnostics/admitted"
    trap 'printf "foreground\n" >> "$TEST_RECORDS/events"; exit 0' TERM
    while :; do sleep 0.05; done
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
    printf '%s\0' "${captured_grants[@]}" > "$TEST_RECORDS/$role.grants"
    printf '%s\0' "${captured_command[@]}" > "$TEST_RECORDS/$role.argv"
    printf '%s\n' "${7-service}" > "$TEST_RECORDS/$role.profile"
    provider_host_ids[$role]=$(printf '%064d' 1)
    case "$role" in
        peer) printf '{}\n' > "$provider_host_root/peer/ready.json" ;;
        selection)
            printf '%s\n' "$TEST_KEY" > "$provider_host_root/selection/key_file.txt"
            printf '%s\n' "$TEST_CA" > "$provider_host_root/selection/ca_file.txt"
            printf '{}\n' > "$provider_host_root/selection/interpreter-inputs.json"
            ;;
        stage) printf 'nmr.localhost\n' > "$provider_host_root/source/host.txt" ;;
        gui-proxy) printf '40123\n' > "$provider_host_root/gui-route/port" ;;
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
            if [[ ${directory##*/} == browser && ${TEST_BROWSER_FAILED-false} == true ]]; then code=1; fi
            printf '%064d|exited|false|%s\n' 1 "$code" > "$directory/observation.stdout"
            printf '%s|%s\n' "${directory##*/}" "$((deadline - SECONDS))" >> "$TEST_RECORDS/waits"
            ;;
        signal) printf '%s\n' '{"requests": 1, "handlers_settled": true}' > "$provider_host_root/peer/retired.json" ;;
    esac
}
"""


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
                argv[-10:],
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
                ],
            )
            self.assertEqual((output / "resolv.conf").read_text(), "nameserver 192.0.2.53\nnameserver 198.51.100.53\n")
            waits = dict(line.split("|", 1) for line in (records / "waits").read_text().splitlines())
            self.assertIn(int(waits["probe"]), (2420, 2419))
            self.assertEqual((records / "removals").read_text().splitlines(), ["selection", "admission", "probe", "proxy"])
            self.assertIn("no API Jobs or science", result.stdout)
            self.assertFalse((records / "foreground.env").exists())

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
            self.assertFalse((records / "foreground.env").exists())

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
        for mode, clocks in (
            ("scripted-pasted", (180, 240, 260)),
            ("real-pasted", (900, 960, 980)),
            ("real-smiles", (900, 960, 980)),
            ("real-smiles-multiple", (900, 960, 980)),
            ("real-upload", (900, 1020, 1040)),
            ("gui-pasted", (900, 1200, 1220)),
            ("gui-smiles", (900, 1200, 1220)),
            ("gui-sdf-zip", (900, 1200, 1220)),
            ("gui-sdf-zip-failed", (900, 1200, 1220)),
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                repository, records, output = root / "repository", root / "records", root / "output"
                records.mkdir()
                browser_failed = mode.endswith("-failed")
                gui_case = mode.removeprefix("gui-").removesuffix("-failed") if mode.startswith("gui-") else None
                script_mode = "real-gui" if gui_case else mode
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
                    "tools/provider_image.sh",
                    "tools/provider_logging.sh",
                    "tools/bound_http_proxy.py",
                    "tools/provider_proxy.py",
                    "tools/container_guard.py",
                    "tests/integration/provider_runtime/gui_proxy.py",
                    "tests/integration/provider_runtime/gui_submission.mjs",
                    "tests/integration/provider_runtime/gui_submission_requests.mjs",
                    "tests/integration/provider_runtime/gui_science.mjs",
                    "tests/integration/provider_runtime/gui_result.mjs",
                    "packages/provider-core/tests/__init__.py",
                    "packages/provider-core/tests/fakes/__init__.py",
                    "tests/fixtures/preparation/gui_sdf.json",
                    "tests/fixtures/preparation/normative/butane_conformers.sdf",
                ):
                    target = repository / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.touch()
                image = "sha256:" + "a" * 64
                image_record = repository / ".work/provider/image-id"
                image_record.parent.mkdir(parents=True)
                image_record.write_text(image + "\n")
                if gui_case:
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
                    (root / "user.json").write_text("{}")
                    (root / "user.json").chmod(0o600)
                    (root / "tls.crt").write_text("authored-public-certificate")
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
                    "GUI_ORIGIN": "https://nmr.localhost:10443",
                    "GUI_CASE": gui_case or "",
                    "USER_CREDENTIAL": str(root / "user.json"),
                    "TLS_CERTIFICATE": str(root / "tls.crt"),
                    "BROWSER_IMAGE": image,
                }
                selected_key, selected_ca = str(root / "selected-key"), str(root / "selected-ca")
                environment = {
                    "PATH": os.environ["PATH"],
                    "TEST_RECORDS": str(records),
                    "TEST_KEY": selected_key,
                    "TEST_CA": selected_ca,
                    "TEST_BROWSER_FAILED": str(browser_failed).lower(),
                }
                environment.update({"NMRNET_" + name + "_INPUT": value for name, value in selectors.items()})
                result = subprocess.run(
                    ["bash", str(script), script_mode], env=environment, capture_output=True, text=True, timeout=10, check=False
                )
                self.assertEqual(result.returncode, 2 if browser_failed else 0, result.stderr)
                foreground = dict(item.split("=", 1) for item in (records / "foreground.env").read_text().split("\0") if item)
                self.assertEqual(foreground["NMRNET_MAPPED_INTERFACE_INPUT"], "lo")
                self.assertEqual(foreground["NMRNET_MAPPED_ADDRESSES_INPUT"], "nmr.localhost=127.0.0.1")
                self.assertEqual(foreground["NMRNET_HOST_DRAIN_SECONDS_INPUT"], "90")
                if gui_case:
                    self.assertEqual((records / "browser.profile").read_text(), "browser\n")
                    self.assertEqual(
                        [line.split("|")[0] for line in (records / "creates").read_text().splitlines()],
                        ["selection", "stage", "gui-proxy", "browser"],
                    )
                    self.assertEqual((records / "events").read_text().splitlines()[-3:], ["browser", "gui-proxy", "foreground"])
                    browser_grants = (records / "browser.grants").read_text().split("\0")[:-1]
                    self.assertFalse(
                        any(
                            selectors["PROVIDER_CREDENTIAL"] in grant or selected_key in grant or selected_ca in grant
                            for grant in browser_grants
                        )
                    )
                    self.assertIn(f"type=bind,src={root}/user.json,dst=/input/user.json,readonly", browser_grants)
                    self.assertIn(f"type=bind,src={root}/tls.crt,dst=/input/tls.crt,readonly", browser_grants)
                    self.assertEqual(
                        any("dst=/fixture/butane_conformers.sdf," in grant for grant in browser_grants), gui_case == "sdf-zip"
                    )
                    self.assertTrue(
                        any(
                            f"src={output}/qualification-source/tests/integration/provider_runtime,dst=/work/proof,readonly"
                            == grant.removeprefix("type=bind,")
                            for grant in browser_grants
                        )
                    )
                    self.assertEqual((records / "browser.argv").read_text().split("\0")[-3:-1], ["--case-kind", gui_case])
                    waits = dict(line.split("|", 1) for line in (records / "waits").read_text().splitlines())
                    self.assertIn(int(waits["browser"]), (1220, 1219))
                    capture = (records / "capture.paths").read_text().splitlines()
                    self.assertIn("tests/integration/provider_runtime/gui_submission.mjs", capture)
                    self.assertIn(
                        "tests/fixtures/preparation/"
                        + ("gui_sdf.json" if gui_case == "sdf-zip" else "api_" + gui_case + ".json"),
                        capture,
                    )
                    continue
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
