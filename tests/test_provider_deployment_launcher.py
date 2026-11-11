"""Exercise maintained Compose commands with a finite recording Engine.

The real host entrypoint owns selection, literal argv and helper retirement.
No Engine, network, API or scientific execution is granted to these fixtures.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "sha256:" + "a" * 64
CID = "b" * 64


class DeploymentLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "checkout"
        self.root.mkdir(mode=0o700)
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir(mode=0o700)
        tools = self.root / "tools"
        tools.mkdir()
        (self.root / "host").mkdir()
        shutil.copyfile(ROOT / "host/compose_image_ids.py", self.root / "host/compose_image_ids.py")
        for name in ("compose_deployment.sh", "provider_deployment.sh", "image_lifecycle.sh", "user_namespace_map.sh"):
            shutil.copyfile(ROOT / "tools" / name, tools / name)
        # Tests can opt into a rootless host mapping. The map parser itself is
        # exercised against synthetic maps by its own tests.
        (tools / "user_namespace_map.sh").write_text("""
require_initial_user_and_pid_namespaces() { return 0; }
probe_process_starttime() { printf '123\\n'; }
probe_process_has_token() { return 0; }
probe_process_is_same() { return 0; }
wait_for_probe_process_retirement() { return 0; }
container_id_for_host_id() { printf '%s\\n' "$2"; }
host_id_for_container_id() {
    if [[ ${TEST_MAPPED_IDENTITIES-false} != true ]]; then printf '%s\\n' "$2";
    elif [[ $2 == 0 ]]; then printf '%s\\n' "$TEST_CALLER_UID";
    else printf '%s\\n' "$((100000 + $2))"; fi
}
""")
        (
            tools / "docker_client.sh"
        ).write_text("""docker_client_prepare() { engine=(bash "$repository_root/tools/record-engine.sh" "$repository_root"); }
docker_client_cleanup() { printf 'client-cleanup\\n' >> "$repository_root/cleanup"; }
""")
        (tools / "inference_native.sh").write_text("""inference_native() {
    local operation=$1 seconds=$2 result=$3 status=0; shift 3
    inference_diagnostic=
    printf '%s|%s|%s\\n' "$operation" "$seconds" "$result" >> "$repository_root/budgets"
    if [[ $result == image-id ]]; then inference_result=$("$@") || status=$?; else "$@" || status=$?; fi
    if [[ $operation == 'old API route inventory' && " $* " == *' inspect --format '* ]]; then
        inference_retirement_confirmed=${TEST_INSPECT_RETIRED-${TEST_RETIRED-true}}
    elif [[ $operation == 'old API route inventory' ]]; then
        inference_retirement_confirmed=${TEST_INVENTORY_RETIRED-${TEST_RETIRED-true}}
    elif [[ $operation == 'deployment generation inventory' && " $* " == *' inspect --format '* ]]; then
        inference_retirement_confirmed=${TEST_GENERATION_INSPECT_RETIRED-${TEST_RETIRED-true}}
    elif [[ $operation == 'provider stop inspection' && " $* " == *' ps --quiet '* ]]; then
        inference_retirement_confirmed=${TEST_PROJECT_RETIRED-${TEST_RETIRED-true}}
    elif [[ $operation == 'retire deployment identity probe' ]]; then
        inference_retirement_confirmed=${TEST_MAP_RETIRED-true}
    elif [[ " $* " == *' ps --all --no-trunc --filter id='* ]]; then
        inference_retirement_confirmed=${TEST_MAP_RETIRED-true}
    elif [[ $operation == 'retire deployment configuration helper' ]]; then
        inference_retirement_confirmed=${TEST_RETIRED-true}
    elif [[ " $* " == *' nmrnet-map-'* || " $* " == *' 0000000000000000000000000000000000000000000000000000000000000006 '* ]]; then
        inference_retirement_confirmed=${TEST_MAP_RETIRED-true}
    elif [[ " $* " == *' run --rm --pull never --name nmrnet-config-'* ]]; then
        inference_retirement_confirmed=${TEST_RETIRED-true}
    else
        inference_retirement_confirmed=${TEST_RETIRED-true}
    fi
    if [[ $status != 0 ]]; then
        if [[ $operation == 'retire deployment configuration helper' ]]; then
            inference_diagnostic=${TEST_CLEANUP_DIAGNOSTIC-}
        else inference_diagnostic=${TEST_PRIMARY_DIAGNOSTIC-}; fi
    fi
    return "$status"
}
""")
        (tools / "provider_image.sh").write_text("provider_image_inputs=(tools/provider_deployment.py)\n")
        (tools / "geometry_image.sh").write_text("geometry_image_inputs=(tools/provider_deployment.py)\n")
        (tools / "git_metadata.sh").write_text("""repository_git() {
    case " $* " in
      *' rev-parse '*) printf '%040d\\n' 1 ;;
      *' --name-only '*) printf 'tools/provider_deployment.py\\n' ;;
      *) printf '160000 commit %040d\\tNMRNetplusplus\\n' 2 ;;
    esac
}
""")
        (tools / "provider_deployment.py").write_text("# fixture source\n")
        (tools / "record-engine.sh").write_text(r"""root=$1; shift
count=0
[[ ! -f $root/count ]] || read -r count < "$root/count"
count=$((count + 1)); printf '%s\n' "$count" > "$root/count"
printf '%s\0' "$@" > "$root/call-$count"
case $1 in
 create) [[ ${TEST_CREATE_STATUS-0} == 0 ]] || exit "$TEST_CREATE_STATUS"; printf '%064d\n' 6 ;;
 start) ;;
 ps)
    [[ ${TEST_PS_STATUS-0} == 0 ]] || exit "$TEST_PS_STATUS"
    if [[ " $* " == *' --filter volume='* ]]; then
        [[ ${TEST_VOLUME_PS_STATUS-0} == 0 ]] || exit "$TEST_VOLUME_PS_STATUS"
        printf '%s' "${TEST_VOLUME_BORROWERS-}"
    elif [[ " $* " == *' --all '* ]]; then printf '%s' "${TEST_CONTAINERS-}";
    elif [[ ${TEST_LIVE-false} == true ]]; then printf '%064d\n' 7; fi
    ;;
 run)
    if [[ ${TEST_RUN_STATUS-0} != 0 ]]; then printf 'actual helper cause\n' >&2; exit "$TEST_RUN_STATUS"; fi
    if [[ " $* " == *' provider_core.deployment_binding paths '* ]]; then
        printf '%s\0' "$root/secrets/deployments/fixture/signing.private.json"
    elif [[ " $* " == *' lifetime-owner '* ]]; then
        printf 'sha256:%064d nmr-api-delivery-control-%064d %s %s\n' 9 1 "$(id -u)" "$(id -g)"
    fi
    if [[ " $* " == *' prepare-runtime '* && " $* " != *' --check-only '* ]]; then
        for role in prediction reader smiles route-interpreter route-store; do
            mkdir -p "$root/secrets/deployments/fixture/runtime/$role"
            touch "$root/secrets/deployments/fixture/runtime/$role/.owner-session.lock"
            chmod 600 "$root/secrets/deployments/fixture/runtime/$role/.owner-session.lock"
        done
    fi
    if [[ " $* " == *' prepare-runtime '* && " $* " == *' --check-only '* ]]; then
        for role in prediction reader smiles route-interpreter route-store; do
            lock="$root/secrets/deployments/fixture/runtime/$role/.owner-session.lock"
            [[ ! -L $lock ]] || { printf 'Cannot prepare provider runtime: native lifetime locks must be standalone regular files.\n' >&2; exit 2; }
        done
    fi
    [[ " $* " != *' old-api-route '* ]] || exit "${TEST_ROUTE_ADMISSION_STATUS-0}"
    arguments=("$@")
    for ((i=0;i<${#arguments[@]};i++)); do
      case ${arguments[i]} in
        --bundle)
          destination=${arguments[i+1]}; mkdir -p "$destination/bindings"
          source="$root/secrets/deployments/fixture/current/compose.json"
          [[ $source -ef $destination/compose.json ]] || cp "$source" "$destination/compose.json"
          printf '[delivery]\n' > "$destination/daemon.toml"
          chmod 444 "$destination/compose.json"; chmod 555 "$destination"
          ;;
        --output) destination=${arguments[i+1]}; mkdir -p "$destination/bindings"; printf '{}\n' > "$destination/compose.json"; printf '[delivery]\n' > "$destination/daemon.toml" ;;
      esac
    done
    ;;
 inspect)
    [[ ${TEST_INSPECT_STATUS-0} == 0 ]] || exit "$TEST_INSPECT_STATUS"
    if [[ " $* " == *'.Config.Labels'* && " $* " == *'.Image'* ]]; then
        case ${6-} in
            *7) service=provider ;;
            *8) service=prediction ;;
            *9) service=reader ;;
            *10) service=smiles ;;
            *11) service=interpreter ;;
            *12) service=store ;;
            *) exit 91 ;;
        esac
        if [[ -v TEST_RUNNING_IMAGE ]]; then
            image=$TEST_RUNNING_IMAGE
        else
            case $service in
                prediction) image=${TEST_PREDICTION_IMAGE-sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa} ;;
                reader|smiles) image=${TEST_GEOMETRY_IMAGE-sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa} ;;
                *) image=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa ;;
            esac
        fi
        printf '%s %s\n' "$service" "$image"
    elif [[ " $* " == *'.State.Pid'* ]]; then printf '1 true\n'
    elif [[ " $* " == *'.State.Status'* ]]; then
        if [[ -f $root/provider-stopped ]]; then printf '%s\n' "${TEST_PROVIDER_STOPPED_STATE-exited 0 false}";
        else printf '%s\n' "${TEST_PROVIDER_STATE-running 0 false}"; fi
    elif [[ " $* " == *'.Mounts'* ]]; then
        if [[ ${TEST_INVENTORY_LIMIT-false} == true ]]; then head -c 1048576 /dev/zero
        else printf '%s' "${TEST_MOUNTS-}"; fi
    else printf 'sha256:%064d\n' 9; fi
    ;;
 image)
    if [[ ${2-} == inspect && ${4-} == '{{.Id}}' ]]; then
        printf '%s\n' "${5-}"
        [[ ${5-} != ${TEST_IMAGE_INSPECT_FAILURE-} ]] || exit 7
    else printf 'sha256:%064d\n' 9; fi
    ;;
 compose)
    if [[ ${TEST_PAUSE-false} == true ]]; then
      : > "$root/ready"
      until [[ -f $root/release ]]; do sleep 0.01; done
    fi
    if [[ " $* " == *' --no-trunc provider '* ]]; then
        printf '%064d\n' 7
        [[ ${TEST_COMPOSE_VERIFICATION_STATUS-0} == 0 ]] || exit "$TEST_COMPOSE_VERIFICATION_STATUS"
    elif [[ " $* " == *' --no-trunc prediction '* ]]; then printf '%064d\n' 8
    elif [[ " $* " == *' --no-trunc reader '* ]]; then printf '%064d\n' 9
    elif [[ " $* " == *' --no-trunc smiles '* ]]; then printf '%064d\n' 10
    elif [[ " $* " == *' --no-trunc interpreter '* ]]; then printf '%064d\n' 11
    elif [[ " $* " == *' --no-trunc store '* ]]; then printf '%064d\n' 12
    elif [[ " $* " == *' ps --all --quiet provider '* ]]; then
        if [[ ! -f $root/provider-removed ]]; then
            provider=${TEST_PROVIDER_ID-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb}
            [[ -z $provider ]] || printf '%s\n' "$provider"
        fi
    elif [[ " $* " == *' ps --all --quiet prediction '* ]]; then printf '%064d\n' 8;
    elif [[ " $* " == *' ps --all --quiet '* ]]; then printf '%s' "${TEST_PROJECT_CONTAINERS-}"; fi
    if [[ ${TEST_COMPOSE_STATUS-0} != 0 ]]; then printf 'actual Compose cause\n' >&2; exit "$TEST_COMPOSE_STATUS"; fi
    if [[ " $* " == *' down '* ]]; then : > "$root/provider-removed"; fi
    ;;
 stop)
    if [[ ${TEST_STOP_PAUSE-false} == true ]]; then
        : > "$root/stop-entered"
        until [[ -f $root/stop-release ]]; do sleep 0.01; done
    fi
    [[ ${TEST_STOP_STATUS-0} == 0 ]] || exit "$TEST_STOP_STATUS"
    : > "$root/provider-stopped"
    ;;
 rm)
    if [[ ${3-} =~ ^0{63}6$ ]]; then [[ ${TEST_MAP_RM_STATUS-0} == 0 ]] || exit "$TEST_MAP_RM_STATUS"
    else [[ ${TEST_RM_STATUS-0} == 0 ]] || exit "$TEST_RM_STATUS"; fi
    ;;
 *) exit 91 ;;
esac
""")
        for kind in ("provider", "inference", "preparation"):
            directory = self.root / ".work" / kind
            directory.mkdir(parents=True)
            (directory / "image-id").write_text(IMAGE + "\n")
        (self.root / "containers/inference").mkdir(parents=True)
        (self.root / "containers/inference/image-inputs.txt").write_text("tools/provider_deployment.py\n")
        definitions = self.root / "config/deployments/fixture"
        definitions.mkdir(parents=True)
        for name in ("provider.toml", "deployment.toml"):
            (definitions / name).write_text("fixture\n")
        self.provider = self.root / "secrets/deployments/fixture"
        self.provider.mkdir(parents=True, mode=0o700)
        (self.provider / "signing.private.json").write_text("private credential\n")
        (self.provider / "unselected-directory").mkdir(mode=0o700)
        (self.provider / "unselected-directory/canary").write_text("never grant this root\n")
        self.bundle = self.selected(self.provider)

    def selected(self, root):
        bundles = root / "bundles"
        bundles.mkdir(mode=0o700)
        bundle = bundles / "generation-original"
        bundle.mkdir(mode=0o700)
        roles = ("provider", "prediction", "reader", "smiles", "interpreter", "store")
        (bundle / "compose.json").write_text(json.dumps({"services": {role: {"image": IMAGE} for role in roles}}) + "\n")
        (bundle / "daemon.toml").write_text("[delivery]\n")
        (root / "current").symlink_to("bundles/generation-original")
        return bundle

    def current_target(self):
        descriptor = os.open(self.provider, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return os.readlink("current", dir_fd=descriptor)
        finally:
            os.close(descriptor)

    def test_current_target_preserves_exact_link_spelling(self):
        current = self.provider / "current"
        current.unlink()
        current.symlink_to("bundles/./generation-original")
        self.assertEqual("bundles/./generation-original", self.current_target())

    def existing_runtime(self):
        runtime = self.provider / "runtime"
        runtime.mkdir(mode=0o700)
        for role in ("prediction", "reader", "smiles", "route-interpreter", "route-store"):
            directory = runtime / role
            directory.mkdir(mode=0o700)
            (directory / ".owner-session.lock").write_text("")
            (directory / ".owner-session.lock").chmod(0o600)

    def run_command(self, action, role="provider", *, changes=None, extra=()):
        environment = dict(
            os.environ,
            HOME=str(self.home),
            NMRNET_DEPLOYMENT_INPUT="fixture",
            NMRNET_SHARED_RUNTIME_IMAGE_INPUT="sha256:" + format(9, "064d"),
            TEST_CALLER_UID=str(os.getuid()),
            TEST_CALLER_GID=str(os.getgid()),
        )
        environment.update(changes or {})
        return subprocess.run(
            ["bash", str(self.root / "tools" / f"{role}_deployment.sh"), action, *extra],
            env=environment,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def remove_retained_identity_metadata(self, result):
        prefix = "Identity inspection metadata retained at "
        for line in result.stderr.splitlines():
            if line.startswith(prefix):
                Path(line.removeprefix(prefix)).unlink(missing_ok=True)

    def calls(self):
        count = int((self.root / "count").read_text()) if (self.root / "count").exists() else 0
        return [
            [item.decode() for item in (self.root / f"call-{index}").read_bytes().split(b"\0")[:-1]]
            for index in range(1, count + 1)
        ]

    def test_missing_or_executable_names_refuse_before_any_engine_call(self):
        for name in ("", "../fixture", "fixture; touch sentinel", "$(touch sentinel)", "x\nup"):
            with self.subTest(name=name):
                result = self.run_command("up", changes={"NMRNET_DEPLOYMENT_INPUT": name})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.calls(), [])
                self.assertFalse((self.root / "sentinel").exists())

    def test_direct_commands_keep_literal_recipe_project_and_finite_options(self):
        expected = {
            "up": ["up", "--detach", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "120"],
            "down": ["down", "--timeout", "60"],
            "status": ["ps", "--all"],
            "logs": ["logs", "--no-color", "--tail", "80"],
        }
        for role in ("provider",):
            for action, suffix in expected.items():
                with self.subTest(role=role, action=action):
                    result = self.run_command(action, role)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = self.calls()
                    call = (
                        next((item for item in calls if item[0] == "compose" and "up" in item), calls[-1])
                        if action == "up"
                        else calls[-1]
                    )
                    project = "nmrnet-" + hashlib.sha256((str(self.root) + "\0fixture").encode()).hexdigest()[:20]
                    bundle = self.bundle
                    directory = self.root
                    self.assertEqual(
                        call,
                        [
                            "compose",
                            "--env-file",
                            "/dev/null",
                            "--project-name",
                            project,
                            "--project-directory",
                            str(directory),
                            "--file",
                            str(bundle / "compose.json"),
                            *suffix,
                        ],
                    )
                    if action == "up":
                        self.assertIn("Verified running image IDs for all 6 provider services.", result.stdout)
                        self.assertEqual(
                            len([item for item in calls if item[0] == "inspect" and ".Config.Labels" in " ".join(item)]), 6
                        )
                    self.assertTrue((bundle / "compose.json").exists())
        self.assertFalse(any("--volumes" in call or "-v" in call for call in self.calls()))
        self.assertTrue((self.root / "cleanup").exists())

    def test_update_preflights_the_generation_then_drains_applies_and_verifies_under_one_lock(self):
        self.existing_runtime()
        scientific_image = "sha256:" + "b" * 64
        geometry_image = "sha256:" + "c" * 64
        (self.root / ".work/inference/image-id").write_text("sha256:" + "d" * 64 + "\n")
        (self.root / ".work/preparation/image-id").write_text("sha256:" + "e" * 64 + "\n")
        plan = json.loads((self.bundle / "compose.json").read_text())
        plan["services"]["prediction"]["image"] = scientific_image
        plan["services"]["reader"]["image"] = geometry_image
        plan["services"]["smiles"]["image"] = geometry_image
        (self.bundle / "compose.json").write_text(json.dumps(plan) + "\n")
        result = self.run_command(
            "update",
            changes={
                "TEST_PREDICTION_IMAGE": scientific_image,
                "TEST_GEOMETRY_IMAGE": geometry_image,
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Verified running image IDs for all 6 provider services.", result.stdout)
        self.assertIn("Provider image update completed", result.stdout)
        calls = self.calls()
        check = next(
            index for index, call in enumerate(calls) if call[0] == "run" and "prepare-runtime" in call and "--check-only" in call
        )
        check_call = calls[check]
        freeze = next(index for index, call in enumerate(calls) if call[0] == "run" and " freeze " in f" {' '.join(call)} ")
        stop = next(index for index, call in enumerate(calls) if call[0] == "stop")
        down = next(index for index, call in enumerate(calls) if call[0] == "compose" and "down" in call)
        apply = next(
            index
            for index, call in enumerate(calls)
            if call[0] == "run" and "prepare-runtime" in call and "--check-only" not in call
        )
        start = next(index for index, call in enumerate(calls) if call[0] == "compose" and "up" in call)
        freeze_call = calls[freeze]
        probe_create = next(call for call in calls if call[0] == "create")
        self.assertLess(check, freeze)
        self.assertLess(freeze, stop)
        self.assertLess(stop, down)
        self.assertLess(down, apply)
        self.assertLess(apply, start)
        self.assertIn("type=bind,src=" + str(self.provider / "runtime") + ",dst=/runtime,readonly", check_call)
        self.assertIn("DAC_OVERRIDE", check_call)
        self.assertNotIn("CHOWN", check_call)
        self.assertEqual(freeze_call[freeze_call.index("--image-id") + 1], scientific_image)
        self.assertEqual(freeze_call[freeze_call.index("--geometry-image-id") + 1], geometry_image)
        self.assertEqual(probe_create[probe_create.index("--pull") + 1], "never")
        self.assertNotEqual(self.current_target(), "bundles/generation-original")

    def test_update_admits_every_recorded_scientific_image_before_draining(self):
        self.existing_runtime()
        inference_image = "sha256:" + "b" * 64
        plan = json.loads((self.bundle / "compose.json").read_text())
        plan["services"]["prediction"]["image"] = inference_image
        (self.bundle / "compose.json").write_text(json.dumps(plan) + "\n")
        result = self.run_command("update", changes={"TEST_IMAGE_INSPECT_FAILURE": inference_image})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[0] == "stop" for call in self.calls()))
        self.assertEqual(self.current_target(), "bundles/generation-original")
        self.assertTrue(any(call[0:5] == ["image", "inspect", "--format", "{{.Id}}", inference_image] for call in self.calls()))

    def test_running_image_verification_rejects_partial_stdout_from_failed_engine_client(self):
        result = self.run_command("up", changes={"TEST_COMPOSE_VERIFICATION_STATUS": "7"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call[0] == "inspect" and ".Config.Labels" in " ".join(call) for call in self.calls()))

    def test_update_holds_deployment_lock_across_drain_and_start(self):
        self.existing_runtime()
        environment = dict(
            os.environ,
            HOME=str(self.home),
            NMRNET_DEPLOYMENT_INPUT="fixture",
            TEST_CALLER_UID=str(os.getuid()),
            TEST_CALLER_GID=str(os.getgid()),
            TEST_STOP_PAUSE="true",
        )
        process = subprocess.Popen(
            ["bash", str(self.root / "tools/provider_deployment.sh"), "update"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 3
            while not (self.root / "stop-entered").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / "stop-entered").exists())
            before = len(self.calls())
            competing = self.run_command("status")
            self.assertEqual(competing.returncode, 2)
            self.assertIn("another command is active", competing.stderr)
            self.assertEqual(len(self.calls()), before)
            (self.root / "stop-release").touch()
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertIn("Provider image update completed", stdout)
        finally:
            (self.root / "stop-release").touch()
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=2)

    def test_down_confirms_exact_provider_exit_before_stopping_dependencies(self):
        result = self.run_command("down")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        stops = [index for index, call in enumerate(calls) if call[0] == "stop"]
        self.assertEqual(len(stops), 1)
        self.assertEqual(calls[stops[0]], ["stop", "--signal", "SIGTERM", "--timeout", "-1", CID])
        teardown = next(index for index, call in enumerate(calls) if "down" in call)
        self.assertLess(stops[0], teardown)
        self.assertTrue(any(".State.Status" in " ".join(call) for call in calls[stops[0] + 1 : teardown]))
        self.assertTrue((self.bundle / "compose.json").exists())
        self.assertFalse(any("--volumes" in call or "--force" in call for call in calls))

    def test_unconfirmed_stop_never_stops_dependencies_or_removes_containers(self):
        for status in (124, 130, 143):
            with self.subTest(status=status):
                before = len(self.calls())
                result = self.run_command("down", changes={"TEST_STOP_STATUS": str(status)})
                self.assertEqual(result.returncode, status, result.stderr)
                calls = self.calls()[before:]
                self.assertEqual(len([call for call in calls if call[0] == "stop"]), 1)
                self.assertFalse(any("down" in call or call[0] == "rm" for call in calls))
                self.assertTrue(self.bundle.exists())
                self.assertIn("no dependencies were stopped", result.stderr)

    def test_stop_requires_known_successful_provider_exit(self):
        for state in ("exited 1 false", "exited 137 true", "running 0 false", "PRIVATE malformed"):
            with self.subTest(state=state):
                before = len(self.calls())
                result = self.run_command("down", changes={"TEST_PROVIDER_STOPPED_STATE": state})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any("down" in call or call[0] == "rm" for call in self.calls()[before:]))
                self.assertNotIn("PRIVATE", result.stderr)

    def test_existing_abnormal_paused_or_restarting_provider_is_not_torn_down(self):
        for state in ("exited 1 false", "exited 137 true", "paused 0 false", "restarting 1 false", "created 0 false"):
            with self.subTest(state=state):
                before = len(self.calls())
                result = self.run_command("down", changes={"TEST_PROVIDER_STATE": state})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any("down" in call or call[0] in ("stop", "rm") for call in self.calls()[before:]))
                self.assertTrue(self.bundle.exists())

    def test_stopped_provider_and_repeated_down_need_no_stop_signal(self):
        for _ in range(2):
            result = self.run_command("down", changes={"TEST_PROVIDER_STATE": "exited 0 false"})
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(call[0] == "stop" for call in self.calls()))

    def test_absent_provider_does_not_authorize_teardown_of_remaining_containers(self):
        result = self.run_command("down", changes={"TEST_PROVIDER_ID": "", "TEST_PROJECT_CONTAINERS": "c" * 64 + "\n"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("down" in call or call[0] in ("stop", "rm") for call in self.calls()))

    def test_ambiguous_provider_selection_cannot_stop_another_container(self):
        for selection in (CID + "\n" + "c" * 64, "PRIVATE invalid"):
            with self.subTest(selection=selection):
                before = len(self.calls())
                result = self.run_command("down", changes={"TEST_PROVIDER_ID": selection})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any("down" in call or call[0] in ("stop", "rm") for call in self.calls()[before:]))
                self.assertNotIn("PRIVATE", result.stderr)

    def test_unretired_query_prevents_following_engine_calls(self):
        result = self.run_command("down", changes={"TEST_RETIRED": "false"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertFalse((self.root / "cleanup").exists())

    def test_interrupting_stop_preserves_dependencies(self):
        environment = dict(os.environ, HOME=str(self.home), NMRNET_DEPLOYMENT_INPUT="fixture", TEST_STOP_PAUSE="true")
        process = subprocess.Popen(
            ["bash", str(self.root / "tools/provider_deployment.sh"), "down"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 2
            while not (self.root / "stop-entered").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / "stop-entered").exists())
            process.send_signal(signal.SIGTERM)
            (self.root / "stop-release").touch()
            _stdout, stderr = process.communicate(timeout=2)
            self.assertEqual(process.returncode, 143, stderr)
            self.assertFalse(any("down" in call or call[0] == "rm" for call in self.calls()))
        finally:
            (self.root / "stop-release").touch()
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=2)

    def test_failed_compose_preserves_original_status_and_selected_configuration_without_retry(self):
        result = self.run_command("up", changes={"TEST_COMPOSE_STATUS": "7"})
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(len([call for call in self.calls() if call[0] == "compose"]), 1)
        self.assertIn("actual Compose cause", result.stderr)
        self.assertIn("No automatic retry", result.stderr)
        self.assertEqual(self.current_target(), "bundles/generation-original")

    def test_up_rejects_a_running_service_with_an_image_other_than_its_frozen_plan(self):
        result = self.run_command("up", changes={"TEST_RUNNING_IMAGE": "sha256:" + "b" * 64})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("started provider container does not use its installed immutable image", result.stderr)
        self.assertIn("Inspect Docker Compose status/logs", result.stderr)
        self.assertFalse(any("down" in call for call in self.calls()))

    def test_up_refuses_existing_project_before_initialization_or_compose(self):
        result = self.run_command("up", changes={"TEST_CONTAINERS": CID + "\n"})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("down", result.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.calls()[0][0:3], ["ps", "--all", "--quiet"])
        self.assertRegex(self.calls()[0][4], r"^label=com\.docker\.compose\.project=nmrnet-[0-9a-f]{20}$")

    def test_up_refuses_failed_or_unretired_project_inspection(self):
        for changes in ({"TEST_PS_STATUS": "7"}, {"TEST_RETIRED": "false"}):
            with self.subTest(changes=changes):
                before = len(self.calls())
                result = self.run_command("up", changes=changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(self.calls()[before:]), 1)

    def test_up_refuses_lifetime_volume_borrowers_before_initialization(self):
        result = self.run_command("up", changes={"TEST_VOLUME_BORROWERS": CID + "\n"})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("lifetime volume is attached", result.stderr)
        volume = "nmr-api-delivery-control-" + "0" * 63 + "1"
        self.assertIn(["ps", "--all", "--quiet", "--filter", f"volume={volume}"], self.calls())
        self.assertFalse(any("initialize-lifetime" in call or "up" in call for call in self.calls()))

    def test_up_refuses_failed_lifetime_borrower_inspection(self):
        result = self.run_command("up", changes={"TEST_VOLUME_PS_STATUS": "7"})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("initialize-lifetime" in call or "up" in call for call in self.calls()))

    def test_completed_command_exits_without_reading_new_checkout_tail(self):
        environment = dict(os.environ, HOME=str(self.home), NMRNET_DEPLOYMENT_INPUT="fixture", TEST_PAUSE="true")
        process = subprocess.Popen(
            ["bash", str(self.root / "tools/provider_deployment.sh"), "status"],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 2
            while not (self.root / "ready").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue((self.root / "ready").exists())
            with (self.root / "tools/compose_deployment.sh").open("a") as changed:
                changed.write('\nprintf replayed > "$repository_root/sentinel"\n')
            (self.root / "release").touch()
            _stdout, stderr = process.communicate(timeout=2)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertFalse((self.root / "sentinel").exists())
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_install_refuses_live_project_without_shutdown_or_replacement(self):
        result = self.run_command("install", changes={"TEST_LIVE": "true"})
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.calls()[0][0:2], ["ps", "--quiet"])
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("stop this deployment", result.stderr)
        self.assertEqual(self.current_target(), "bundles/generation-original")

    def test_install_reuses_the_current_generation_shared_runtime_image(self):
        result = self.run_command("install", changes={"NMRNET_SHARED_RUNTIME_IMAGE_INPUT": ""})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertTrue(any(call[0:5] == ["image", "inspect", "--format", "{{.Id}}", IMAGE] for call in calls))
        self.assertTrue(any("--shared-image-id" in call and IMAGE in call for call in calls))

    def test_install_can_retain_previous_generations(self):
        original = self.provider / "bundles/generation-original"
        obsolete = self.provider / "bundles/generation-obsolete"
        obsolete.mkdir()
        result = self.run_command("install", changes={"NMRNET_RETAIN_PRIOR_INPUT": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(original.is_dir())
        self.assertTrue(obsolete.is_dir())
        self.assertIn("Retained previous configuration generations.", result.stdout)

    def test_identity_probe_create_failure_does_not_remove_an_unadmitted_name(self):
        result = self.run_command("install", changes={"TEST_CREATE_STATUS": "7"})
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertFalse(any(call[0] == "rm" for call in self.calls()))
        self.assertIn("Identity probe, if still present:", result.stderr)
        self.remove_retained_identity_metadata(result)

    def test_identity_probe_removal_is_not_retried_after_an_uncertain_attempt(self):
        result = self.run_command("install", changes={"TEST_MAP_RM_STATUS": "9"})
        self.assertEqual(result.returncode, 9, result.stderr)
        removals = [call for call in self.calls() if call[0] == "rm"]
        self.assertEqual(len(removals), 1)
        self.assertRegex(removals[0][2], r"^[0-9a-f]{64}$")
        self.assertIn("Identity probe ID:", result.stderr)
        self.remove_retained_identity_metadata(result)

    def test_provider_freeze_has_narrow_readonly_sources_and_fixed_helper_grants(self):
        result = self.run_command("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("shared proxy and outbox are running", result.stdout)
        call = next(call for call in self.calls() if "freeze" in call)
        for pair in (
            ["--network", "none"],
            ["--cap-drop", "ALL"],
            ["--pull", "never"],
            ["--security-opt", "no-new-privileges:true"],
            ["--user", f"{os.getuid()}:{os.getgid()}"],
            ["--memory", "512m"],
            ["--memory-swap", "512m"],
            ["--pids-limit", "64"],
        ):
            self.assertIn(pair, [call[index : index + 2] for index in range(len(call) - 1)])
        self.assertIn("--read-only", call)
        self.assertIn("--init", call)
        mounts = [call[index + 1] for index, value in enumerate(call[:-1]) if value == "--mount"]
        self.assertIn(
            f"type=bind,src={self.provider}/signing.private.json,dst={self.provider}/signing.private.json,readonly", mounts
        )
        self.assertFalse(any(f"src={self.provider}," in mount or "unselected-directory" in mount for mount in mounts))
        self.assertIn("freeze", call)
        self.assertIn("--source-root", call)
        self.assertIn("/opt/provider/environment/bin/python", call)
        selected = self.provider / (self.provider / "current").readlink()
        self.assertNotEqual(selected, self.bundle)
        self.assertEqual(selected.stat().st_mode & 0o777, 0o555)
        self.assertEqual((selected / "compose.json").stat().st_mode & 0o777, 0o444)
        self.assertFalse(self.bundle.exists())
        probes = [call for call in self.calls() if call[0] in ("create", "start")]
        self.assertEqual([call[0] for call in probes], ["create", "start"])
        admitted_id = f"{6:064d}"
        self.assertEqual(probes[1][1], admitted_id)
        inspected = next(
            call for call in self.calls() if call[0] == "inspect" and any(".State.Pid" in argument for argument in call)
        )
        self.assertEqual(inspected[-1], admitted_id)
        removed = next(call for call in self.calls() if call[0] == "rm")
        self.assertEqual(removed[-1], admitted_id)
        freeze_user = next(call[call.index("--user") + 1] for call in self.calls() if "freeze" in call)
        self.assertEqual(freeze_user, f"{os.getuid()}:{os.getgid()}")

    def test_install_accepts_existing_runtime_leaves_owned_by_mapped_identity(self):
        runtime_root = self.provider / "runtime"
        runtime_root.mkdir(mode=0o700)
        for role in ("prediction", "reader", "smiles", "route-interpreter", "route-store"):
            (runtime_root / role).mkdir(mode=0o700)
        mapped_uid = os.getuid() + 100000
        mapped_gid = os.getgid() + 100000
        fake_bin = self.root / "fake-bin"
        fake_bin.mkdir()
        fake_stat = fake_bin / "stat"
        fake_stat.write_text("""#!/usr/bin/env bash
if [[ ${1-} == -c && ${2-} == '%u:%g:%a' && ${4-} == "$TEST_RUNTIME_ROOT/"* ]]; then
    printf '%s:%s:700\\n' "$TEST_MAPPED_UID" "$TEST_MAPPED_GID"
else
    exec /usr/bin/stat "$@"
fi
""")
        fake_stat.chmod(0o755)
        result = self.run_command(
            "install",
            changes={
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "TEST_MAPPED_IDENTITIES": "true",
                "TEST_RUNTIME_ROOT": str(runtime_root),
                "TEST_MAPPED_UID": str(mapped_uid),
                "TEST_MAPPED_GID": str(mapped_gid),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installed provider configuration.", result.stdout)

    def test_retirement_preserves_current_stopped_borrowers_and_original_inputs(self):
        bundles = self.provider / "bundles"
        obsolete = bundles / "generation-obsolete"
        obsolete.mkdir(mode=0o700)
        (obsolete / "config").mkdir()
        (obsolete / "config/provider.toml").write_text("derived inputs\n")
        (obsolete / "config").chmod(0o500)
        obsolete.chmod(0o500)
        borrowed = bundles / "generation-stopped"
        borrowed.mkdir()
        (borrowed / "config.toml").write_text("stopped borrower\n")
        (bundles / "generation-symlink").symlink_to(self.provider / "unselected-directory")
        result = self.run_command(
            "retire-generations",
            changes={
                "TEST_CONTAINERS": CID,
                "TEST_MOUNTS": str(borrowed / "config.toml") + "\n",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(obsolete.exists())
        self.assertTrue(borrowed.exists())
        self.assertTrue(self.bundle.exists())
        self.assertTrue((self.provider / "unselected-directory/canary").exists())
        self.assertTrue((self.provider / "signing.private.json").exists())
        self.assertIn(["ps", "--all", "--quiet"], self.calls())
        self.assertIn("Retired 1 unused", result.stdout)

    def test_mount_above_generations_keeps_all_reachable_bundles(self):
        unselected = self.provider / "bundles/generation-borrowed"
        unselected.mkdir()
        result = self.run_command(
            "retire-generations",
            changes={
                "TEST_CONTAINERS": CID,
                "TEST_MOUNTS": str(self.provider) + "\n",
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(unselected.exists())
        self.assertIn("Retired 0 unused", result.stdout)

    def test_inspection_failure_removes_no_generation(self):
        unselected = self.provider / "bundles/generation-unresolved"
        unselected.mkdir()
        result = self.run_command(
            "retire-generations",
            changes={
                "TEST_CONTAINERS": CID,
                "TEST_INSPECT_STATUS": "7",
            },
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertTrue(unselected.exists())
        self.assertTrue(self.bundle.exists())

    def test_retire_api_route_preserves_owned_neighbors_and_recipe(self):
        route = self.provider / "runtime/route-api"
        route.mkdir(parents=True, mode=0o700)
        (route / "ready").write_text("ready\n")
        neighbor = self.provider / "runtime/route-store"
        neighbor.mkdir(mode=0o700)
        result = self.run_command("retire-api-route")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(route.exists())
        self.assertTrue(neighbor.exists())
        self.assertTrue(self.bundle.exists())
        self.assertIn("Retired the old API route", result.stdout)

    def test_install_preserves_old_recipe_until_api_route_is_retired(self):
        route = self.provider / "runtime/route-api"
        route.mkdir(parents=True, mode=0o700)
        result = self.run_command("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retire the old API route", result.stderr)
        self.assertTrue(route.exists())
        self.assertTrue(self.bundle.exists())
        self.assertEqual(self.current_target(), "bundles/generation-original")
        self.assertFalse(any("freeze" in call for call in self.calls()))

    def test_unretired_install_preflight_retains_client_and_recipe(self):
        result = self.run_command("install", changes={"TEST_RETIRED": "false"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call[0] for call in self.calls()], ["ps"])
        self.assertFalse((self.root / "cleanup").exists())
        self.assertTrue(self.bundle.exists())

    def test_unretired_generation_inventory_retains_client_and_bundles(self):
        obsolete = self.provider / "bundles/generation-unused"
        obsolete.mkdir()
        for changes in ({"TEST_RETIRED": "false"}, {"TEST_CONTAINERS": CID, "TEST_GENERATION_INSPECT_RETIRED": "false"}):
            with self.subTest(changes=changes):
                (self.root / "cleanup").unlink(missing_ok=True)
                result = self.run_command("retire-generations", changes=changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((self.root / "cleanup").exists())
                self.assertTrue(obsolete.exists())
                self.assertTrue(self.bundle.exists())

    def test_install_refuses_an_absent_old_route_still_mounted_by_a_container(self):
        route = self.provider / "runtime/route-api"
        result = self.run_command(
            "install",
            changes={
                "TEST_CONTAINERS": CID,
                "TEST_MOUNTS": str(route) + "\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still mounts", result.stderr)
        self.assertTrue(self.bundle.exists())
        self.assertEqual(self.current_target(), "bundles/generation-original")
        self.assertFalse(any("freeze" in call for call in self.calls()))

    def test_retire_api_route_refuses_any_overlapping_container_mount(self):
        route = self.provider / "runtime/route-api"
        route.mkdir(parents=True, mode=0o700)
        for mount in (str(route), str(route / "ready"), str(route.parent), "/"):
            with self.subTest(mount=mount):
                result = self.run_command(
                    "retire-api-route",
                    changes={
                        "TEST_CONTAINERS": CID,
                        "TEST_MOUNTS": mount + "\n",
                    },
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(route.exists())
                self.assertIn("still mounts", result.stderr)

    def test_retire_api_route_retains_directory_when_admission_or_inspection_fails(self):
        route = self.provider / "runtime/route-api"
        route.mkdir(parents=True, mode=0o700)
        for changes in ({"TEST_ROUTE_ADMISSION_STATUS": "7"}, {"TEST_CONTAINERS": CID, "TEST_INSPECT_STATUS": "7"}):
            with self.subTest(changes=changes):
                result = self.run_command("retire-api-route", changes=changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(route.exists())

    def test_absent_api_route_still_refuses_a_container_borrower(self):
        route = self.provider / "runtime/route-api"
        result = self.run_command(
            "retire-api-route",
            changes={
                "TEST_CONTAINERS": CID,
                "TEST_MOUNTS": str(route) + "\n",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("still mounts", result.stderr)
        self.assertFalse(route.exists())
        for changes in (
            {"TEST_PS_STATUS": "7"},
            {"TEST_CONTAINERS": CID, "TEST_INSPECT_STATUS": "7"},
            {"TEST_CONTAINERS": CID, "TEST_INVENTORY_LIMIT": "true"},
        ):
            with self.subTest(changes=changes):
                result = self.run_command("retire-api-route", changes=changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("directory is absent", result.stdout)
        for changes in ({"TEST_RETIRED": "false"}, {"TEST_CONTAINERS": CID, "TEST_INSPECT_RETIRED": "false"}):
            with self.subTest(changes=changes):
                (self.root / "cleanup").unlink(missing_ok=True)
                result = self.run_command("retire-api-route", changes=changes)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("directory is absent", result.stdout)
                self.assertFalse((self.root / "cleanup").exists())
        result = self.run_command("retire-api-route")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("directory is absent", result.stdout)

    def test_native_lifetime_locks_are_created_private_and_keep_their_inode(self):
        result = self.run_command("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        locks = [self.provider / "runtime" / role / ".owner-session.lock" for role in ("prediction", "reader", "smiles")]
        before = [path.stat().st_ino for path in locks]
        for path in locks:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.stat().st_uid, os.getuid())
        result = self.run_command("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([path.stat().st_ino for path in locks], before)

    def test_native_lifetime_lock_symlink_is_refused_before_freeze(self):
        (self.provider / "runtime").mkdir(mode=0o700)
        directory = self.provider / "runtime" / "reader"
        directory.mkdir(mode=0o700)
        target = self.root / "unrelated"
        target.write_text("keep original bytes")
        (directory / ".owner-session.lock").symlink_to(target)
        result = self.run_command("install")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("native lifetime locks", result.stderr)
        self.assertEqual(target.read_text(), "keep original bytes")
        self.assertFalse(any("freeze" in call for call in self.calls()))

    def test_local_deployment_settings_are_passed_to_the_provider_freezer(self):
        result = self.run_command("install")
        self.assertEqual(result.returncode, 0, result.stderr)
        call = next(call for call in self.calls() if "freeze" in call)
        self.assertEqual(call[call.index("--settings") + 1], str(self.root / "config/deployments/fixture/deployment.toml"))
        self.assertNotIn("--shared-settings", call)

    def test_failed_helper_is_removed_by_its_original_name_without_reexecution(self):
        result = self.run_command("install", changes={"TEST_RUN_STATUS": "7"})
        self.assertEqual(result.returncode, 7, result.stderr)
        calls = self.calls()
        runs = [call for call in calls if call[0] == "run"]
        self.assertEqual(len(runs), 1)
        name = runs[0][runs[0].index("--name") + 1]
        self.assertEqual(calls[-1], ["rm", "--force", name])
        self.assertIn("actual helper cause", result.stderr)
        self.assertEqual(self.current_target(), "bundles/generation-original")

    def test_unretired_local_helper_never_dispatches_a_new_cleanup_client(self):
        result = self.run_command(
            "install",
            changes={
                "TEST_RUN_STATUS": "7",
                "TEST_RETIRED": "false",
                "TEST_INVENTORY_RETIRED": "true",
                "TEST_PROJECT_RETIRED": "true",
            },
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(
            [call[0] for call in self.calls()], ["ps", "ps", "image", "image", "create", "start", "inspect", "rm", "ps", "run"]
        )
        self.assertFalse((self.root / "cleanup").exists())
        self.assertIn("Configuration helper, if still present:", result.stderr)

    def test_helper_removal_failure_preserves_the_original_command_failure(self):
        result = self.run_command("install", changes={"TEST_RUN_STATUS": "7", "TEST_RM_STATUS": "9"})
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(self.calls()[-1][0], "rm")

    def test_helper_cleanup_preserves_primary_and_separate_cleanup_diagnostics(self):
        for cleanup_status in ("0", "9"):
            with self.subTest(cleanup_status=cleanup_status):
                before = len(self.calls())
                result = self.run_command(
                    "install",
                    changes={
                        "TEST_RUN_STATUS": "7",
                        "TEST_RM_STATUS": cleanup_status,
                        "TEST_PRIMARY_DIAGNOSTIC": "original native client diagnostic",
                        "TEST_CLEANUP_DIAGNOSTIC": "separate retirement diagnostic",
                    },
                )
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertEqual(result.stderr.count("original native client diagnostic"), 1)
                self.assertEqual(result.stderr.count("separate retirement diagnostic"), int(cleanup_status != "0"))
                self.assertIn("actual helper cause", result.stderr)
                calls = self.calls()[before:]
                runs = [call for call in calls if call[0] == "run"]
                self.assertEqual(len(runs), 1)
                name = runs[0][runs[0].index("--name") + 1]
                self.assertEqual(calls[-1], ["rm", "--force", name])
                self.assertEqual(self.current_target(), "bundles/generation-original")

    def test_client_inputs_uses_selected_prediction_image_and_readonly_bundle(self):
        output = Path(self.temporary.name) / "client"
        result = self.run_command("client-inputs", changes={"NMRNET_OUTPUT_INPUT": str(output)})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(calls[0][-4:], ["ps", "--all", "--quiet", "prediction"])
        self.assertEqual(calls[1][0:3], ["inspect", "--type", "container"])
        call = next(call for call in calls if "client-inputs" in call)
        self.assertIn(f"type=bind,src={self.bundle},dst={self.bundle},readonly", call)
        self.assertEqual(
            call[call.index("client-inputs") + 1 :],
            ["--bundle", str(self.bundle), "--prediction-image-id", "sha256:" + f"{9:064d}", "--client-root", str(output)],
        )

    def test_native_event_projection_and_engine_failure_keep_their_existing_owners(self):
        operation = "a" * 32
        for status in (0, 7):
            script = f"printf '%s\\n' PRIVATE_APPLICATION_OUTPUT 'INFO event=child_started operation={operation} pid=14' 'INFO event=child_retired operation={operation} returncode=-15 reason=owner_lost' >&2; exit {status}"
            result = subprocess.run(
                ["bash", str(ROOT / "tools/compose_runner_events.sh"), "b" * 64, "bash", "-c", script],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            self.assertEqual(result.returncode, status, result.stderr)
            self.assertEqual(result.stdout, f"started\t{operation}\t14\nretired\t{operation}\t-15\towner_lost\n")
            self.assertNotIn("PRIVATE", result.stdout)
            self.assertIn("PRIVATE_APPLICATION_OUTPUT", result.stderr)
