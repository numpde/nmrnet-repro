#!/usr/bin/env python3
"""Model only the engine calls exercised by contained repository-check fixtures.

No Docker socket is accessed. Unexpected commands fail with status 97, following
Magnet's fake-engine rejection contract at commit 2d4ee50b0359e71900fce9289ea01f46d0f41249.
"""

from tools.container_guard import require_container

require_container()

import json
import os
import stat
import sys
from pathlib import Path

from repository_checks.fixtures.containers.fixture import record_engine_call

# Metadata and diagnostics retain print's single trailing LF. The byte-count
# fault output below deliberately writes without a newline; keep that distinct.
arguments = sys.argv[1:]
configuration = None
global_options = set()
while arguments and arguments[0] in {"--host", "--config"}:
    option = arguments[0]
    if len(arguments) < 2 or option in global_options:
        sys.stderr.write("unexpected fake docker invocation: missing or repeated global option" + "\n")
        raise SystemExit(97)
    global_options.add(option)
    if option == "--config":
        path = arguments[1]
        if not Path(path).is_absolute():
            sys.stderr.write("unexpected fake docker invocation: client config must be absolute" + "\n")
            raise SystemExit(97)
        try:
            metadata = os.lstat(path)
        except OSError as error:
            sys.stderr.write("unexpected fake docker invocation: client config cannot be inspected" + "\n")
            raise SystemExit(97) from error
        configuration = {
            "path": path,
            "uid": metadata.st_uid,
            "mode": stat.S_IMODE(metadata.st_mode),
            # silent-defaults: allow-contract -- Non-directories are recorded as other; the consumer requires a directory independently of its entries; proof=repository_checks/test_inference_client_config.py::InferenceClientConfigTests.test_build_and_import_select_empty_owned_configuration_then_retire_it
            "kind": "directory" if stat.S_ISDIR(metadata.st_mode) else "other",
            # silent-defaults: allow-contract -- Non-directories have no enumerated entries; the separate kind assertion prevents this empty observation from certifying a directory; proof=repository_checks/test_inference_client_config.py::InferenceClientConfigTests.test_build_and_import_select_empty_owned_configuration_then_retire_it
            "entries": sorted(entry.name for entry in Path(path).iterdir()) if stat.S_ISDIR(metadata.st_mode) else [],
        }
    elif arguments[1] != "unix:///var/run/docker.sock":
        sys.stderr.write("unexpected fake docker invocation: unsupported engine endpoint" + "\n")
        raise SystemExit(97)
    arguments = arguments[2:]
log_path = os.environ["NMRNET_FAKE_ENGINE_LOG"]
record_engine_call(
    Path(log_path),
    {
        "arguments": arguments,
        "client_config": configuration,
        "environment": {
            name: value
            for name, value in os.environ.items()
            if name.startswith(("DOCKER_", "BUILDX_")) or name.lower().endswith("_proxy")
        },
    },
)
# silent-defaults: allow-contract -- Empty argv selects only the rejection sentinel, never a supported engine verb; proof=repository_checks/test_engine_metadata_fixture.py::EngineMetadataFixtureTests.test_unknown_formats_and_nonexact_argv_reject_before_fault_responses
command = arguments[0] if arguments else ""
for fault_name, accepted_values in (
    ("NMRNET_FAKE_CREATE_ROLE", {"proxy", "worker"}),
    ("NMRNET_FAKE_HOLD_CREATE", {"1"}),
    ("NMRNET_FAKE_PROXY_NO_READY", {"1"}),
):
    if fault_name in os.environ and os.environ[fault_name] not in accepted_values:
        sys.stderr.write("unexpected fake docker invocation: unmodeled acquisition fault selector\n")
        raise SystemExit(97)
if command in {"build", "create", "start", "rm"}:
    log = Path(log_path)
if command == "build":
    import hashlib
    import shlex

    context = Path(arguments[-1])
    named_contexts = [arguments[index + 1] for index, value in enumerate(arguments) if value == "--build-context"]
    if len(named_contexts) != 1 or not named_contexts[0].startswith("wheelhouse="):
        sys.stderr.write("unexpected fake docker invocation: one wheelhouse context is required" + "\n")
        raise SystemExit(97)
    wheelhouse = Path(named_contexts[0].removeprefix("wheelhouse="))
    dockerfile = Path(arguments[arguments.index("--file") + 1])
    if not context.is_dir() or not dockerfile.is_file() or not dockerfile.is_relative_to(context):
        sys.stderr.write("unexpected fake docker invocation: build requires an existing owned context and Dockerfile" + "\n")
        raise SystemExit(97)
    mutation = os.environ.get("NMRNET_FAKE_MUTATE_INPUT")
    if mutation:
        Path(mutation).write_bytes(b"concurrent worktree mutation\n")
        Path(mutation).chmod(0o755)
    manifest = {}
    for instruction in dockerfile.read_text().splitlines():
        if not instruction.startswith("COPY ") or "--from=" in instruction:
            continue
        sources = shlex.split(instruction)[1:-1]
        sources = [source for source in sources if not source.startswith("--")]
        for source in sources:
            path = context / source
            if not path.exists() or path.is_symlink():
                sys.stderr.write("unexpected fake docker invocation: required COPY input is absent or symlinked" + "\n")
                raise SystemExit(97)
            # silent-defaults: allow-contract -- Admitted COPY directories contribute their recursive file leaves; a single-file source contributes that file; proof=repository_checks/test_inference_inputs.py::CapturedInputTests.test_source_executable_mode_is_materialized_from_the_committed_tree
            files = sorted(path.rglob("*")) if path.is_dir() else [path]
            for file in files:
                if file.is_file():
                    manifest[str(file.relative_to(context))] = {
                        "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                        "mode": stat.S_IMODE(file.stat().st_mode),
                    }
    wheelhouse_manifest = {
        file.name: {"sha256": hashlib.sha256(file.read_bytes()).hexdigest(), "mode": stat.S_IMODE(file.stat().st_mode)}
        for file in wheelhouse.iterdir()
    }
    (log.parent / "build-evidence.json").write_text(
        json.dumps(
            {
                "context": str(context),
                "wheelhouse": str(wheelhouse),
                "capture_mode": stat.S_IMODE(context.parent.stat().st_mode),
                "capture_uid": context.parent.stat().st_uid,
                "directory_modes": {
                    str(directory.relative_to(context)): stat.S_IMODE(directory.stat().st_mode)
                    for directory in sorted(context.rglob("*"))
                    if directory.is_dir()
                },
                "manifest": manifest,
                "wheelhouse_manifest": wheelhouse_manifest,
                "content_identity": hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest(),
            }
        )
    )
    if os.environ.get("NMRNET_FAKE_BUILD_BYTES"):
        build_output = "x" * int(os.environ["NMRNET_FAKE_BUILD_BYTES"])
    else:
        build_output = os.environ["NMRNET_FAKE_BUILD_RESULT"]
    sys.stdout.write(build_output + "\n")
    if os.environ.get("NMRNET_FAKE_BUILD_STATUS"):
        raise SystemExit(int(os.environ["NMRNET_FAKE_BUILD_STATUS"]))
elif arguments[:2] == ["image", "inspect"]:
    # Model exact projections requested by tools/baseline.sh metadata admission
    # and tools/inference_image.sh identity admission. A substring mentioning
    # the label cannot authorize a different format or extra argv.
    if len(arguments) == 3 and arguments[2] and not arguments[2].startswith("-"):
        projection = "{{.Id}}"
    elif len(arguments) == 5 and arguments[2] == "--format" and arguments[4] and not arguments[4].startswith("-"):
        projection = arguments[3]
    else:
        sys.stderr.write("unexpected fake docker invocation: unmodeled image inspect argv\n")
        raise SystemExit(97)
    label_projection = '{{ index .Config.Labels "io.nmrnet-repro.upstream-revision" }}'
    if projection not in {"{{.Id}}", label_projection, "{{.Id}}|" + label_projection}:
        sys.stderr.write("unexpected fake docker invocation: unmodeled image inspect format\n")
        raise SystemExit(97)
    if os.environ.get("NMRNET_FAKE_INSPECT_PRIVATE_ERROR"):
        sys.stderr.write(os.environ["NMRNET_FAKE_INSPECT_PRIVATE_ERROR"] + "\n")
    if os.environ.get("NMRNET_FAKE_INSPECT_STATUS"):
        raise SystemExit(int(os.environ["NMRNET_FAKE_INSPECT_STATUS"]))
    identity = os.environ["NMRNET_FAKE_IMAGE_ID"]
    revision = os.environ["NMRNET_FAKE_IMAGE_REVISION"]
    if projection == "{{.Id}}":
        response = identity
    elif projection == label_projection:
        response = revision
    else:
        response = identity + "|" + revision
    sys.stdout.write(response + "\n")
elif command == "create":
    import re

    state = log.parent / "containers.json"
    identities = json.loads(state.read_text())
    proxy = any(argument.startswith("type=bind,src=") and argument.endswith(",dst=/ready") for argument in arguments)
    # silent-defaults: allow-contract -- A ready bind selects the modeled proxy role; the supported request without it is a worker; proof=repository_checks/test_engine_metadata_fixture.py::EngineMetadataFixtureTests.test_owned_empty_registry_preserves_two_container_lifetimes_and_native_statuses
    role = "proxy" if proxy else "worker"
    identifier = os.environ[f"NMRNET_FAKE_{role.upper()}_CID"]
    receipt = Path(arguments[arguments.index("--cidfile") + 1])
    if os.environ.get("NMRNET_FAKE_CREATE_ROLE") == role:
        if "NMRNET_FAKE_CREATE_OUTPUT" not in os.environ or os.environ["NMRNET_FAKE_CREATE_OUTPUT"] not in {
            "exact",
            "empty",
            "disagree",
            "nul",
            "cap",
        }:
            sys.stderr.write("unexpected fake docker invocation: unmodeled create output fault\n")
            raise SystemExit(97)
        output_kind = os.environ["NMRNET_FAKE_CREATE_OUTPUT"]
        if (
            "NMRNET_FAKE_CREATE_STATUS" not in os.environ
            or re.fullmatch(r"0|[1-9][0-9]{0,2}", os.environ["NMRNET_FAKE_CREATE_STATUS"]) is None
            or int(os.environ["NMRNET_FAKE_CREATE_STATUS"]) > 255
        ):
            sys.stderr.write("unexpected fake docker invocation: create fault status must be from 0 through 255\n")
            raise SystemExit(97)
        create_fault_status = int(os.environ["NMRNET_FAKE_CREATE_STATUS"])
    if os.environ.get("NMRNET_FAKE_NUL_RECEIPT_ROLE") == role:
        # Environment strings cannot carry NUL. The fixture emits the exact
        # formerly admitted 65-byte receipt at the file-writing boundary.
        receipt.write_bytes(identifier.encode("ascii") + b"\0")
    else:
        receipt.write_text(identifier + "\n")
    (log.parent / (role + "-cid-path")).write_text(str(receipt))
    if re.fullmatch("[0-9a-f]{64}", identifier):
        # silent-defaults: allow-contract -- Each admitted identity retains its modeled create role for subsequent start matching; proof=repository_checks/test_engine_metadata_fixture.py::EngineMetadataFixtureTests.test_owned_empty_registry_preserves_two_container_lifetimes_and_native_statuses
        identities[identifier] = "proxy" if proxy else "worker"
    state.write_text(json.dumps(identities))
    for argument in arguments:
        if argument.startswith("type=bind,src=") and argument.endswith(",dst=/ready"):
            (log.parent / "ready-path").write_text(argument.split("src=", 1)[1].split(",dst=", 1)[0])
    if os.environ.get("NMRNET_FAKE_CREATE_ROLE") == role:
        # Emit bytes before the real cancellation marker. The controller must
        # retain contradictory stdout even when the exact CID already exists.
        if output_kind == "exact":
            create_output = identifier.encode("ascii") + b"\n"
        elif output_kind == "empty":
            create_output = b""
        elif output_kind == "disagree":
            # silent-defaults: allow-contract -- Emit a valid 64-hex CID different from the selected identity: e unless that identity is all e, then f; proof=repository_checks/test_acquisition_client_config.py::AcquisitionClientTests.test_actual_create_cancel_never_erases_disagreeing_or_nul_stdout
            create_output = (b"e" if identifier != "e" * 64 else b"f") * 64 + b"\n"
        elif output_kind == "nul":
            create_output = identifier.encode("ascii") + b"\0"
        elif output_kind == "cap":
            create_output = b"x" * 4096
        sys.stdout.buffer.write(create_output)
        sys.stdout.buffer.flush()
        if os.environ.get("NMRNET_FAKE_HOLD_CREATE") == "1":
            import signal

            (log.parent / "create-held").write_text(role + "\n" + str(os.getpid()) + "\n")
            while True:
                signal.pause()
        raise SystemExit(create_fault_status)
    sys.stdout.write(identifier + "\n")
elif command == "start":
    identities = json.loads((log.parent / "containers.json").read_text())
    # silent-defaults: allow-contract -- Attached starts require worker identities and unattached starts require proxy identities before modeled effects; proof=repository_checks/test_engine_metadata_fixture.py::EngineMetadataFixtureTests.test_owned_empty_registry_preserves_two_container_lifetimes_and_native_statuses
    expected_role = "worker" if "--attach" in arguments else "proxy"
    if identities.get(arguments[-1]) != expected_role:
        sys.stderr.write("unexpected fake docker invocation: unknown container identity" + "\n")
        raise SystemExit(97)
    if "--attach" not in arguments:
        path = Path((log.parent / "ready-path").read_text())
        if os.environ.get("NMRNET_FAKE_PROXY_NO_READY") != "1":
            (path / "port").write_text("12345\n")
    else:
        if "NMRNET_FAKE_WORKER_STDOUT" in os.environ:
            sys.stdout.write(os.environ["NMRNET_FAKE_WORKER_STDOUT"])
            sys.stdout.flush()
        if "NMRNET_FAKE_WORKER_STDERR" in os.environ:
            sys.stderr.write(os.environ["NMRNET_FAKE_WORKER_STDERR"])
            sys.stderr.flush()
        if os.environ.get("NMRNET_FAKE_HOLD_ATTACH") == "1":
            import signal

            def retire_attach(signum, _frame):
                (log.parent / "attach-retired").write_text(str(os.getpid()))
                raise SystemExit(128 + signum)

            signal.signal(signal.SIGTERM, retire_attach)
            (log.parent / "attach-ready").write_text(str(os.getpid()))
            while True:
                signal.pause()
        if os.environ.get("NMRNET_FAKE_CORRUPT_COMPLETED_WORKER_RECEIPT"):
            # This exact start client has passed identity admission. Corrupt its
            # receipt only at ordinary completion, independently of cancellation.
            receipt = Path((log.parent / "worker-cid-path").read_text())
            receipt.write_text(os.environ["NMRNET_FAKE_CORRUPT_COMPLETED_WORKER_RECEIPT"] + "\n")
            (log.parent / "attach-completed").write_text(str(os.getpid()))
        raise SystemExit(int(os.environ["NMRNET_FAKE_WORKER_STATUS"]))
elif command == "inspect":
    sys.stdout.write(os.environ["NMRNET_FAKE_PROXY_STATE"] + "\n")
    raise SystemExit(int(os.environ["NMRNET_FAKE_PROXY_INSPECT_STATUS"]))
elif command == "rm":
    state = log.parent / "containers.json"
    identities = json.loads(state.read_text())
    if arguments[-1] not in identities:
        sys.stderr.write("unexpected fake docker invocation: unknown container identity" + "\n")
        raise SystemExit(97)
    if os.environ["NMRNET_FAKE_REMOVE_STATUS"] == "0":
        identities.pop(arguments[-1])
        state.write_text(json.dumps(identities))
    raise SystemExit(int(os.environ["NMRNET_FAKE_REMOVE_STATUS"]))
elif command == "logs":
    sys.stderr.write("private native fixture output" + "\n")
    raise SystemExit(int(os.environ["NMRNET_FAKE_PROXY_LOG_STATUS"]))
elif command == "run":
    if os.environ.get("NMRNET_FAKE_VERIFY_WHEELHOUSE") == "1" and "tools.wheelhouse" in arguments:
        import subprocess

        mounts = {}
        for index, argument in enumerate(arguments):
            if argument != "--mount":
                continue
            fields = dict(field.split("=", 1) for field in arguments[index + 1].split(",") if "=" in field)
            mounts[fields["dst"]] = fields["src"]
        frozen_context = str(Path(mounts["/workspace/tools/wheelhouse.py"]).parent.parent)
        selected = arguments[arguments.index("python3") :]
        # silent-defaults: allow-contract -- Translate exact mounted path tokens and retain literal verifier argv tokens; proof=repository_checks/test_inference_inputs.py::CapturedInputTests.test_frozen_verifier_checks_actual_copied_wheel_bytes_before_build
        selected = [mounts.get(value, value) for value in selected]
        selected[0] = sys.executable
        raise SystemExit(
            subprocess.run(selected, cwd=frozen_context, env=os.environ | {"PYTHONPATH": frozen_context}, check=False).returncode
        )
    if os.environ.get("NMRNET_FAKE_HOLD_MODE"):
        import subprocess
        import time

        mode = os.environ["NMRNET_FAKE_HOLD_MODE"]
        if mode == "eof":
            os.close(1)
            os.close(2)
            # Publish only after entering the fault. A deadline during Python
            # startup must not be mistaken for proof of EOF handling.
            (Path(log_path).parent / "fault-entered").write_text("eof\n")
            time.sleep(60)
        if mode in {"failed-leader", "held-pipe"}:
            # silent-defaults: allow-contract -- The closed failed-leader mode discards both descendant streams; held-pipe inherits them to keep native output open until retirement; proof=repository_checks/test_operator_inputs_port.py::NativeCommandPostureTests.test_failed_leader_and_held_pipe_retire_surviving_native_descendant
            descendant_output = subprocess.DEVNULL if mode == "failed-leader" else None
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(60)"],
                stdout=descendant_output,
                stderr=descendant_output,
            )
            with (Path(log_path).parent / "descendant.pid").open("w") as receipt:
                receipt.write(str(child.pid))
            # Both the child and its exact PID receipt precede this marker;
            # status 7 alone cannot establish that retirement was exercised.
            (Path(log_path).parent / "fault-entered").write_text(mode + "\n")
            raise SystemExit(7)
        time.sleep(60)
    if os.environ.get("NMRNET_FAKE_OUTPUT"):
        sys.stdout.write(os.environ["NMRNET_FAKE_OUTPUT"] + "\n")
    if os.environ.get("NMRNET_FAKE_OUTPUT_BYTES"):
        sys.stdout.write("x" * int(os.environ["NMRNET_FAKE_OUTPUT_BYTES"]))
    if os.environ.get("NMRNET_FAKE_EMPTY_DISCOVERY") == "1":
        import re
        import shlex
        import subprocess
        import tempfile

        # This process is already contained. Model the selected container's
        # private /tmp so its wrapper cannot collide with the outer checker's
        # HOME; discovery alone receives the empty proof directory. No other
        # Python or shell command is admitted by this execution adapter.
        empty = os.environ["NMRNET_FAKE_EMPTY_DIRECTORY"]
        evidence = Path(log_path).parent / "discovery-evidence.jsonl"
        # Identify the command after the actual image selector, never an argv
        # token inside another command. Only this launcher's two-token run flags
        # and standalone switches are modeled; a new grant needs explicit review.
        switches = {"--rm", "--read-only", "--init"}
        valued = {
            "--network",
            "--pull",
            "--env",
            "--user",
            "--cap-drop",
            "--security-opt",
            "--cpus",
            "--memory",
            "--memory-swap",
            "--pids-limit",
            "--tmpfs",
            "--workdir",
            "--mount",
        }
        index = 1
        while index < len(arguments) and arguments[index].startswith("--"):
            if arguments[index] in switches:
                index += 1
            elif (
                arguments[index] in valued
                and index + 1 < len(arguments)
                and arguments[index + 1]
                and not arguments[index + 1].startswith("--")
            ):
                index += 2
            else:
                sys.stderr.write("unexpected fake docker invocation: unmodeled discovery run flag" + "\n")
                raise SystemExit(97)
        python_base = "docker.io/library/python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c"
        if index >= len(arguments) or (
            arguments[index] != python_base and re.fullmatch(r"sha256:[0-9a-f]{64}", arguments[index]) is None
        ):
            sys.stderr.write("unexpected fake docker invocation: unmodeled discovery image selector" + "\n")
            raise SystemExit(97)
        original = arguments[index + 1 :]
        if original[:1] == ["python3"]:
            if original != ["python3", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]:
                sys.stderr.write("unexpected fake docker invocation: unmodeled Python discovery argv" + "\n")
                raise SystemExit(97)
            mapped = original.copy()
            mapped[6] = empty
            completed = subprocess.run(mapped, check=False)
            record = {"original": original, "mapped": mapped, "status": completed.returncode}
        elif original[:1] == ["bash"]:
            wrapper = (
                'mkdir -m 700 /tmp/checker-home; printf "%s\\n" "[safe]" '
                '"directory = /workspace" "directory = /workspace/NMRNetplusplus" '
                "> /tmp/checker-home/.gitconfig; exec python3 -B -m unittest discover -s repository_checks -v"
            )
            homes = [
                arguments[index + 1]
                for index, value in enumerate(arguments[:-1])
                if value == "--env" and arguments[index + 1].startswith("HOME=")
            ]
            if original != ["bash", "-eu", "-c", wrapper] or homes != ["HOME=/tmp/checker-home"]:
                sys.stderr.write("unexpected fake docker invocation: unmodeled checker discovery wrapper" + "\n")
                raise SystemExit(97)
            with tempfile.TemporaryDirectory(prefix="fake-container-home-", dir=evidence.parent) as directory:
                home = Path(directory) / "checker-home"
                mapped = original.copy()
                mapped[3] = wrapper.replace("/tmp/checker-home", shlex.quote(str(home)))
                mapped[3] = mapped[3].removesuffix("repository_checks -v") + shlex.quote(empty) + " -v"
                completed = subprocess.run(mapped, env=os.environ | {"HOME": str(home)}, check=False)
                record = {
                    "original": original,
                    "mapped": mapped,
                    "status": completed.returncode,
                    "home": str(home),
                    "home_mode": stat.S_IMODE(home.stat().st_mode),
                    "home_config": (home / ".gitconfig").read_text(),
                }
        else:
            sys.stderr.write("unexpected fake docker invocation: no modeled discovery command" + "\n")
            raise SystemExit(97)
        with evidence.open("a") as output:
            output.write(json.dumps(record) + "\n")
        raise SystemExit(completed.returncode)
else:
    sys.stderr.write(("unexpected fake docker invocation: " + " ".join(arguments)) + "\n")
    raise SystemExit(97)
raise SystemExit(int(os.environ["NMRNET_FAKE_ENGINE_STATUS"]))
