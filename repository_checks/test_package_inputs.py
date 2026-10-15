#!/usr/bin/env python3
"""Observe admitted package bytes through finite build and actual lock producers."""

from tools.container_guard import require_container

require_container()

import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path

from repository_checks import test_packages_client_config as clients


class PackageInputCaptureTests(unittest.TestCase):
    _remove_retained = clients.PackagesClientTests._remove_retained
    _remember = clients.PackagesClientTests._remember
    caller = clients.PackagesClientTests.caller
    owned_calls = clients.PackagesClientTests.owned_calls

    def setUp(self):
        clients.PackagesClientTests.setUp(self)
        (self.root / "bin/docker").unlink()
        (self.root / "bin/docker").symlink_to(Path(__file__).resolve())
        # Keep the base fixture's exact pinned-image admission; this adapter
        # replaces only captured-worker execution, not controller preflight.
        client_controller = self.root / "tools/docker.package-client.sh"
        (self.root / "tools/docker.sh").rename(client_controller)
        (self.root / "tools/docker.sh").write_text(
            '#!/bin/bash\nif [[ "$1" == run ]]; then shift;\n'
            f'    exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} controller "$@"\n'
            "fi\n"
            f'exec bash {shlex.quote(str(client_controller))} "$@"\n'
        )
        self.intent = {
            "inference.in": b"torch @ https://fixture.test/torch-2.13.0%2Bcpu-cp312-cp312-manylinux_2_28_x86_64.whl#sha256="
            + b"a" * 64
            + b"\n# edited intent\n",
            "build.in": b"# edited build intent\n",
            "unicore-source.json": b'{"edited":true}\n',
        }
        for name, content in self.intent.items():
            (self.root / "requirements" / name).write_bytes(content)
            (self.root / "requirements" / name).chmod(0o640)

    def stage_packages(self, *, additions=None):
        return self.caller("stage", environment=self.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo"} | (additions or {}))

    def apply_packages(self, stage, *, additions=None):
        return self.caller("apply", environment=self.environment | {"NMRNET_STAGE_INPUT": stage.name} | (additions or {}))

    def captured_stage(self):
        found = [path for path in self.stage_directory().glob("lock.*") if path.name != "lock.fixture"]
        self.assertEqual(1, len(found))
        return found[0]

    def stage_directory(self):
        return self.root / ".work/packages-stage"

    def expected_hashes(self):
        return {name: hashlib.sha256(content).hexdigest() for name, content in self.intent.items()}

    def test_stage_uses_one_retained_capture_for_context_intent_and_real_report(self):
        result = self.stage_packages(additions={"NMRNET_CAPTURE_MUTATE_LIVE": "1"})
        self.assertEqual(0, result.returncode, msg=result.stderr)
        stage = self.captured_stage()
        capture = stage / ".inputs"
        self.assertEqual(0o700, stat.S_IMODE(capture.stat().st_mode))
        self.assertEqual(
            {
                "containers/packages/Dockerfile.lock",
                "containers/packages/Dockerfile.lock.dockerignore",
                "tools/container_guard.py",
                "tools/package_lock.py",
            },
            {str(path.relative_to(capture / "context")) for path in (capture / "context").rglob("*") if path.is_file()},
        )
        report = json.loads((stage / "advisories.json").read_bytes())
        self.assertEqual(self.expected_hashes(), report["inputs_sha256"])
        build = json.loads((self.root / "capture-build.json").read_text())
        controller = json.loads((self.root / "capture-controller.json").read_text())
        worker = json.loads((self.root / "worker-stage.json").read_text())
        self.assertEqual(str(capture), build["capture"])
        self.assertEqual(str(capture), controller["capture"])
        self.assertEqual(self.expected_hashes(), worker["inputs_sha256"])
        self.assertEqual(self.expected_hashes(), {name: value["sha256"] for name, value in controller["inputs"].items()})
        self.assertTrue(all(value["mode"] == 0o640 for value in controller["inputs"].values()))
        self.assertEqual(build["tools_sha256"], worker["tools_sha256"])
        for name, evidence in build["execution"].items():
            self.assertEqual(hashlib.sha256((Path.cwd() / name).read_bytes()).hexdigest(), evidence["sha256"])
            self.assertEqual(0o644, evidence["mode"])
        for name, content in self.intent.items():
            captured = capture / "requirements" / name
            self.assertEqual(content, captured.read_bytes())
            self.assertEqual(0o640, stat.S_IMODE(captured.stat().st_mode))
            self.assertEqual(b"mutation after admission\n", (self.root / "requirements" / name).read_bytes())
        self.assertEqual("# mutated live execution\n", (self.root / "tools/package_lock.py").read_text())
        self.assertEqual("sha256:" + "b" * 64 + "\n", (stage / "image-id").read_text())

    def test_apply_uses_fresh_current_intent_and_captured_execution_for_actual_pair(self):
        result = self.stage_packages()
        self.assertEqual(0, result.returncode, msg=result.stderr)
        stage = self.captured_stage()
        outputs = self.root / "requirements"
        (outputs / "inference.lock").write_bytes(b"previous lock\n")
        (outputs / "advisories.json").write_bytes(b"previous advisory\n")
        (outputs / "build.in").write_bytes(b"changed after staging\n")
        rejected = self.apply_packages(stage)
        self.assertEqual(1, rejected.returncode, msg=rejected.stderr)
        self.assertIn("dependency inputs changed after staging", rejected.stderr)
        self.assertEqual(b"previous lock\n", (outputs / "inference.lock").read_bytes())
        self.assertEqual(b"previous advisory\n", (outputs / "advisories.json").read_bytes())
        self.assertFalse(list(self.stage_directory().glob(".apply.*")))
        (outputs / "build.in").write_bytes(self.intent["build.in"])
        # Old stage intent cannot authorize apply. It is deliberately unrelated
        # now; current admission matches the reviewed report independently.
        (stage / ".inputs/requirements/build.in").write_bytes(b"old stage is not current\n")
        applied = self.apply_packages(stage, additions={"NMRNET_CAPTURE_MUTATE_LIVE": "1"})
        self.assertEqual(0, applied.returncode, msg=applied.stderr)
        self.assertEqual((stage / "inference.lock").read_bytes(), (outputs / "inference.lock").read_bytes())
        self.assertEqual((stage / "advisories.json").read_bytes(), (outputs / "advisories.json").read_bytes())
        worker = json.loads((self.root / "worker-apply.json").read_text())
        self.assertEqual(self.expected_hashes(), worker["inputs_sha256"])
        self.assertEqual(2, len(worker["tools_sha256"]))
        self.assertFalse(list(self.stage_directory().glob(".apply.*")))
        self.assertTrue((stage / ".inputs").is_dir())

    def test_committed_execution_rejects_dirty_bytes_modes_links_and_wrong_inventory(self):
        for mutation in ("bytes", "mode", "link", "inventory"):
            with self.subTest(mutation=mutation):
                probe = clients.PackagesClientTests()
                probe.setUp()
                try:
                    source = probe.root / "tools/package_lock.py"
                    if mutation == "bytes":
                        source.write_bytes(b"dirty execution\n")
                    elif mutation == "mode":
                        source.chmod(0o755)
                    elif mutation == "link":
                        source.rename(probe.root / "saved-tool")
                        source.symlink_to(probe.root / "saved-tool")
                    else:
                        tree = probe.root / ".fake-git/parent/package-stage.tree"
                        tree.write_bytes(b"\0".join(tree.read_bytes().split(b"\0")[:-2]) + b"\0")
                    result = probe.caller("stage", environment=probe.environment | {"NMRNET_WLAN_INTERFACE_INPUT": "lo"})
                    self.assertEqual(2, result.returncode, msg=result.stderr)
                    self.assertIn("Cannot complete inference package input capture", result.stderr)
                    evidence = re.search(
                        r"private capped output retained at (/tmp/nmrnet-inference-native\.[A-Za-z0-9]+/command.log)",
                        result.stderr,
                    )
                    self.assertIsNotNone(evidence, msg=result.stderr)
                    self.assertIn("Cannot capture package execution inputs", Path(evidence[1]).read_text())
                    self.assertEqual([], probe.owned_calls())
                    self.assertFalse((probe.root / "controller-returned").exists())
                finally:
                    self.assertTrue(probe.doCleanups())

    def test_copy_rejects_persistent_parent_leaf_byte_and_mode_replacements(self):
        for mutation in ("parent", "leaf", "bytes", "mode"):
            with self.subTest(mutation=mutation):
                function = (
                    '() { command cp "$@" || return "$?"; '
                    'if [[ "$*" == *requirements/inference.in* ]]; then '
                    'case "$NMRNET_COPY_MUTATION" in '
                    'parent) mv "$NMRNET_COPY_ROOT/requirements" "$NMRNET_COPY_ROOT/saved-requirements"; '
                    'ln -s "$NMRNET_COPY_ROOT/saved-requirements" "$NMRNET_COPY_ROOT/requirements";; '
                    'leaf) rm -f "${@: -1}"; ln -s "$NMRNET_COPY_ROOT/requirements/inference.in" "${@: -1}";; '
                    'bytes) printf changed >> "$NMRNET_COPY_ROOT/requirements/inference.in";; '
                    'mode) chmod 600 "$NMRNET_COPY_ROOT/requirements/inference.in";; esac; fi; }'
                )
                result = self.stage_packages(
                    additions={"BASH_FUNC_cp%%": function, "NMRNET_COPY_MUTATION": mutation, "NMRNET_COPY_ROOT": str(self.root)}
                )
                self.assertEqual(2, result.returncode, msg=result.stderr)
                self.assertIn("Cannot complete inference package input capture", result.stderr)
                evidence = re.search(
                    r"private capped output retained at (/tmp/nmrnet-inference-native\.[A-Za-z0-9]+/command.log)", result.stderr
                )
                self.assertIsNotNone(evidence, msg=result.stderr)
                self.assertIn("Cannot capture package dependency intent", Path(evidence[1]).read_text())
                self.assertIn("inference.in", Path(evidence[1]).read_text())
                self.assertEqual([], self.owned_calls())
                if mutation == "parent":
                    (self.root / "requirements").unlink()
                    (self.root / "saved-requirements").rename(self.root / "requirements")
                (self.root / "requirements/inference.in").write_bytes(self.intent["inference.in"])
                (self.root / "requirements/inference.in").chmod(0o640)

    def test_apply_unknown_retains_capture_and_cleanup_failure_reports_attempt_truth(self):
        unknown = self.caller(
            "apply", environment=self.environment | {"NMRNET_STAGE_INPUT": "lock.fixture", "NMRNET_PACKAGE_READY_FAULT": "1"}
        )
        self.assertEqual(1, unknown.returncode, msg=unknown.stderr)
        self.assertIn("private apply inputs retained at", unknown.stderr)
        captures = list(self.stage_directory().glob(".apply.*"))
        self.assertEqual(1, len(captures))
        self.assertEqual(0o700, stat.S_IMODE(captures[0].stat().st_mode))
        self.assertEqual([], self.owned_calls())
        for deletion in (False, True):
            with self.subTest(deletion=deletion):
                removal = (
                    '() { if [[ "$*" == *packages-stage/.apply.* ]]; then '
                    + ('command rm "$@"; ' if deletion else "")
                    + 'return 23; fi; command rm "$@"; }'
                )
                # No actual worker effects are needed for this cleanup fault:
                # the original finite client preserves its ordinary status37.
                (self.root / "bin/docker").unlink()
                (self.root / "bin/docker").symlink_to(Path(clients.__file__).resolve())
                (self.root / "native-status").write_text("37")
                result = self.caller(
                    "apply", environment=self.environment | {"NMRNET_STAGE_INPUT": "lock.fixture", "BASH_FUNC_rm%%": removal}
                )
                self.assertEqual(37, result.returncode, msg=result.stderr)
                self.assertIn("Cannot confirm package apply input removal; attempted path", result.stderr)
                self.assertIn("(status 23)", result.stderr)
                self.assertNotIn("private apply inputs retained", result.stderr)
                remaining = [path for path in self.stage_directory().glob(".apply.*") if path != captures[0]]
                self.assertEqual(0 if deletion else 1, len(remaining))
                for path in remaining:
                    subprocess.run(["/bin/rm", "-rf", "--", str(path)], check=True)
        command = [str(self.root / "bin/git"), "-C", str(self.root)]
        short = subprocess.run(
            [*command, "ls-tree", "-r", "-z", "a" * 40], env=self.environment, capture_output=True, check=False
        )
        self.assertEqual(97, short.returncode)
        self.assertEqual(b"", short.stdout)
        self.assertEqual(b"", short.stderr)

    def test_capture_cancellation_with_full_public_stderr_retires_before_cleanup_and_drain(self):
        record = self.stage / "image-id"
        before_record = record.read_bytes(), record.stat().st_ino
        for action in ("stage", "apply"):
            for interrupt, status in ((signal.SIGHUP, 129), (signal.SIGINT, 130), (signal.SIGTERM, 143)):
                with self.subTest(action=action, interrupt=interrupt):
                    ready = self.root / "capture-utility-ready"
                    ready.unlink(missing_ok=True)
                    config_ready = self.root / "capture-config-ready"
                    config_ready.unlink(missing_ok=True)
                    (self.root / "report.json").unlink(missing_ok=True)
                    read_fd, write_fd = os.pipe()
                    process = None
                    try:
                        capacity = fcntl.fcntl(write_fd, fcntl.F_GETPIPE_SZ)
                        os.set_blocking(write_fd, False)
                        for size in (4096, 1):
                            while True:
                                try:
                                    os.write(write_fd, b"x" * size)
                                except BlockingIOError:
                                    break
                        os.set_blocking(write_fd, True)
                        function = (
                            "() { head -c 8388672 /dev/zero >&2; "
                            'printf "%s\\n" "$BASHPID" > "$NMRNET_CAPTURE_UTILITY_READY"; '
                            "while :; do sleep 1; done; }"
                        )
                        process = subprocess.Popen(
                            ["bash", str(self.root / "tools/packages.sh"), action],
                            env=self.environment
                            | {
                                "NMRNET_WLAN_INTERFACE_INPUT": "lo",
                                "NMRNET_STAGE_INPUT": "lock.fixture",
                                "NMRNET_CAPTURE_UTILITY_READY": str(ready),
                                "NMRNET_CAPTURE_CONFIG_READY": str(config_ready),
                                "BASH_FUNC_cp%%": function,
                                "BASH_FUNC_mktemp%%": "() { local directory; "
                                'if directory=$(command mktemp "$@"); then :; else return "$?"; fi; '
                                'if [[ "$*" == *"/tmp/nmrnet-docker-client."* ]]; then '
                                'printf "%s\\n" "$directory" > "$NMRNET_CAPTURE_CONFIG_READY"; fi; '
                                'printf "%s\\n" "$directory"; }',
                            },
                            stdout=subprocess.PIPE,
                            stderr=write_fd,
                            start_new_session=True,
                        )
                        os.close(write_fd)
                        write_fd = None
                        clients.PackagesClientTests._wait_path(self, ready)
                        utility = int(ready.read_text())
                        anchor = os.getpgid(utility)
                        # Rescue admits only this observed candidate after
                        # checking its session, held signature and caller.
                        (self.root / "report.json").write_text(json.dumps({"anchor": anchor}))
                        arguments = Path("/proc", str(anchor), "cmdline").read_bytes().split(b"\0")
                        self.assertEqual([b"bash", b"-c"], arguments[:2])
                        self.assertIn(b"receipt=$1 parent=$2 budget=$3 timed_program=$4", arguments[2])
                        self.assertEqual(str(process.pid).encode(), arguments[5])
                        self.assertEqual(process.pid, os.getsid(anchor))
                        scratch = Path(arguments[4].decode()).parent
                        self.retained.add(scratch)
                        evidence = scratch / "command.log"
                        self.assertGreater(8388672, capacity)
                        deadline = time.monotonic() + 5
                        while evidence.stat().st_size != 8388608 and time.monotonic() < deadline:
                            time.sleep(0.01)
                        self.assertEqual(b"\0" * 8388608, evidence.read_bytes())
                        self.assertEqual(0o700, stat.S_IMODE(scratch.stat().st_mode))
                        worker = Path("/proc", str(utility), "cmdline").read_bytes().split(b"\0")
                        self.assertEqual([b"bash", b"-eu", b"-c"], worker[:3])
                        self.assertIn(b'package_capture_inputs "$2" "$3"', worker[3])
                        self.assertEqual([b"bash", str(self.root).encode(), action.encode()], worker[4:7])
                        capture = Path(worker[7].decode())
                        self.assertTrue(capture.is_dir())
                        config = Path(config_ready.read_text().strip())
                        self.assertTrue((config / "config").is_dir())
                        self.assertEqual([], self.owned_calls())
                        self.assertFalse((self.root / "controller-returned").exists())
                        self.assertIsNone(process.poll())
                        process.send_signal(interrupt)
                        self.assertEqual(status, process.wait(timeout=5))
                        self.assertFalse(Path("/proc", str(utility)).exists())
                        self.assertFalse(Path("/proc", str(anchor)).exists())
                        self.assertEqual(action == "stage", capture.is_dir())
                        self.assertFalse(config.exists())
                        self.assertEqual(before_record, (record.read_bytes(), record.stat().st_ino))
                        self.assertEqual(b"", process.stdout.read())
                        self.assertEqual({ord("x")}, set(os.read(read_fd, capacity)))
                        self.assertEqual([], self.owned_calls())
                    finally:
                        try:
                            if process is not None:
                                clients.PackagesClientTests._retire_failed_probe(self, process)
                                process.communicate(timeout=3)
                        finally:
                            if write_fd is not None:
                                os.close(write_fd)
                            os.close(read_fd)


def mounts(arguments):
    return {
        fields["dst"]: fields
        for index, value in enumerate(arguments)
        if value == "--mount"
        for fields in [dict(part.split("=", 1) if "=" in part else (part, True) for part in arguments[index + 1].split(","))]
    }


def actual_worker(action, arguments):
    root = Path(os.environ["NMRNET_PACKAGE_FIXTURE_ROOT"])
    selected = mounts(arguments)
    inputs = {name: Path(selected["/input/" + name]["src"]) for name in ("inference.in", "build.in", "unicore-source.json")}
    capture = inputs["inference.in"].parent.parent
    assert all(selected["/input/" + name]["readonly"] is True for name in inputs)
    assert all(path.parent.parent == capture for path in inputs.values())
    if action == "stage":
        assert selected["/output/.inputs"] == {"type": "bind", "src": str(capture), "dst": "/output/.inputs", "readonly": True}
    else:
        for name in ("package_lock.py", "container_guard.py"):
            assert selected["/workspace/tools/" + name] == {
                "type": "bind",
                "src": str(capture / "context/tools" / name),
                "dst": "/workspace/tools/" + name,
                "readonly": True,
            }
    if "NMRNET_CAPTURE_MUTATE_LIVE" in os.environ:
        for name in inputs:
            (root / "requirements" / name).write_bytes(b"mutation after admission\n")
        (root / "tools/package_lock.py").write_text("# mutated live execution\n")
        (root / "tools/container_guard.py").write_text("raise RuntimeError('live guard must not execute')\n")
    record = {
        "capture": str(capture),
        "inputs": {
            name: {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mode": stat.S_IMODE(path.stat().st_mode)}
            for name, path in inputs.items()
        },
    }
    if action == "stage":
        (root / "capture-controller.json").write_text(json.dumps(record))
    program = r"""
import hashlib, importlib, json, os, pathlib, re, stat, sys
action, context, inputs, stage, output, receipt = sys.argv[1:]
sys.path.insert(0, context)
module = importlib.import_module("tools.package_lock")
guard = importlib.import_module("tools.container_guard")
assert pathlib.Path(module.__file__) == pathlib.Path(context) / "tools/package_lock.py"
assert pathlib.Path(guard.__file__) == pathlib.Path(context) / "tools/container_guard.py"
roots = {"/input": pathlib.Path(inputs), "/input/inference.in": pathlib.Path(inputs) / "inference.in",
         "/stage": pathlib.Path(stage), "/output": pathlib.Path(output)}
def translated(value):
    assert isinstance(value, str)
    if value in roots:
        return roots[value]
    candidate = pathlib.Path(value)
    assert action == "apply" and candidate.parent == pathlib.Path(output)
    assert candidate.name.startswith(".lock-apply.") and candidate.is_dir() and not candidate.is_symlink()
    assert candidate.stat().st_uid == os.getuid() and stat.S_IMODE(candidate.stat().st_mode) == 0o700
    return candidate
module.Path = translated
record = {"inputs_sha256": module.input_hashes(), "tools_sha256": {
    name: hashlib.sha256((pathlib.Path(context) / name).read_bytes()).hexdigest()
    for name in ("tools/container_guard.py", "tools/package_lock.py")}}
pathlib.Path(receipt).write_text(json.dumps(record))
if action == "stage":
    def resolve(command, **keywords):
        assert command == ["uv", "pip", "compile", "/input/inference.in", "/input/build.in",
            "--python-version", "3.12.12", "--python-platform", "x86_64-manylinux_2_28", "--generate-hashes",
            "--no-build", "--no-emit-index-url", "--no-emit-find-links", "--no-header",
            "--output-file", str(pathlib.Path(output) / "resolved.raw")]
        assert keywords == {"check": True, "stdout": module.subprocess.DEVNULL}
        reference = re.search(r"^torch @ (https://\S+)$", (pathlib.Path(inputs) / "inference.in").read_text(), re.MULTILINE)[1]
        digest = reference.split("#sha256=")[1]
        (pathlib.Path(output) / "resolved.raw").write_text("torch @ " + reference + " \\\n    --hash=sha256:" + digest + "\n")
    module.subprocess.run = resolve
    module.advisory = lambda requirement: {"name": requirement[0], "version": requirement[1],
        "url": "https://fixture.test/advisory", "vulnerabilities": []}
    module.stage()
else:
    module.apply()
"""
    stage = selected["/stage"]["src"] if action == "apply" else selected["/output"]["src"]
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            program,
            action,
            str(capture / "context"),
            str(capture / "requirements"),
            stage,
            selected["/output"]["src"],
            str(root / ("worker-" + action + ".json")),
        ],
        check=False,
    )
    return result.returncode


def fixture_main():
    root = Path(os.environ["NMRNET_PACKAGE_FIXTURE_ROOT"])
    if sys.argv[1:2] == ["controller"]:
        return actual_worker("stage", sys.argv[2:])
    arguments = sys.argv[1:]
    if arguments[:1] == ["--config"]:
        arguments = arguments[2:]
    assert arguments[:2] == ["--host", "unix:///var/run/docker.sock"]
    arguments = arguments[2:]
    status = clients.fake_command("docker")
    if status != 0:
        return status
    if arguments[:1] == ["build"]:
        context = Path(arguments[-1])
        capture = context.parent
        record = {
            "capture": str(capture),
            "tools_sha256": {
                name: hashlib.sha256((context / name).read_bytes()).hexdigest()
                for name in ("tools/container_guard.py", "tools/package_lock.py")
            },
        }
        record["execution"] = {
            str(path.relative_to(context)): {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
            for path in context.rglob("*")
            if path.is_file()
        }
        (root / "capture-build.json").write_text(json.dumps(record))
        if "NMRNET_CAPTURE_MUTATE_LIVE" in os.environ:
            for name in ("inference.in", "build.in", "unicore-source.json"):
                (root / "requirements" / name).write_bytes(b"mutation after admission\n")
            (root / "tools/package_lock.py").write_text("# mutated live execution\n")
            (root / "tools/container_guard.py").write_text("raise RuntimeError('live guard must not execute')\n")
    if arguments[:1] == ["run"]:
        return actual_worker("apply", arguments)
    return 0


if __name__ == "__main__":
    if Path(sys.argv[0]).name == "docker" or sys.argv[1:2] == ["controller"]:
        sys.exit(fixture_main())
    unittest.main()
