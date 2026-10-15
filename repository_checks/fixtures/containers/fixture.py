"""Provide isolated current-launcher copies and a finite offline engine double."""

from tools.container_guard import require_container

require_container()

import fcntl
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from repository_checks.quality.sources import ROOT

FIXTURES = ROOT / "repository_checks/fixtures/containers"


def record_engine_call(log, observed):
    # Event shape: tests/test_checker_engine.py:109-153 at
    # 4490c0ffcf86748b8761becdbfeab284d47c4733.
    # One JSON file per call stays below the client's inherited file-size cap.
    # Package rescue probes can own two simultaneous callers; serialize only
    # observation admission, not their execution, and publish complete bytes.
    descriptor = os.open(log, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        paths = sorted(log.iterdir())
        for index, path in enumerate(paths):
            if path.name != f"{index:06d}.json":
                raise ValueError("Engine call evidence is not a complete ordered event directory.")
        event = log / f"{len(paths):06d}.json"
        pending = event.with_suffix(".pending")
        with pending.open("x") as stream:
            json.dump(observed, stream)
            stream.write("\n")
        pending.replace(event)
    finally:
        os.close(descriptor)


def read_engine_calls(log):
    # Missing directories and malformed/incomplete events remain failed proof,
    # never a reconstructed empty history. The lock excludes an active writer.
    descriptor = os.open(log, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        observed = []
        for index, path in enumerate(sorted(log.iterdir())):
            if path.name != f"{index:06d}.json":
                raise ValueError("Engine call evidence is not a complete ordered event directory.")
            observed.append(json.loads(path.read_text()))
        return observed
    finally:
        os.close(descriptor)


class LauncherFixture:
    def __init__(self, root):
        self.root = root
        self.log = root / "engine.jsonl"
        self.log.mkdir()
        # Start with no containers exactly once. Later missing state is lost
        # proof, rather than permission to recreate an empty resource inventory.
        with (root / "containers.json").open("x") as registry:
            registry.write("{}\n")
        self.scratch = set()
        (root / "tools").mkdir(parents=True)
        for name in (
            "container.sh",
            "docker.sh",
            "docker_client.sh",
            "check_git_inputs.sh",
            "inference_image.sh",
            "inference_native.sh",
            "inference_inputs.sh",
            "git_inputs.sh",
            "git_metadata.sh",
            "packages.sh",
            "package_inputs.sh",
            "baseline.sh",
        ):
            shutil.copyfile(ROOT / "tools" / name, root / "tools" / name)
        shutil.copyfile(ROOT / "Makefile", root / "Makefile")
        shutil.copytree(ROOT / "containers", root / "containers")
        for name in ("container_guard.py", "artifacts.py", "wheelhouse.py", "build_unicore.py", "package_lock.py"):
            shutil.copyfile(ROOT / "tools" / name, root / "tools" / name)
        upstream = root / "NMRNetplusplus/uninmr"
        upstream.mkdir(parents=True)
        (upstream / "infer.py").write_bytes(b"pinned_source = 1\n")
        (root / "NMRNetplusplus/LICENSE").write_bytes(b"upstream license\n")
        # These are finite metadata-composition fixtures, not real Git object
        # stores. The real helper still validates topology and emits its actual
        # sanitized scaffold; test_git_inputs.py separately proves native Git.
        for database in (root / ".git", root / ".git/modules/nmrnet"):
            database.mkdir(parents=True)
            for directory in ("objects", "refs"):
                (database / directory).mkdir()
            (database / "HEAD").write_text("a" * 40 + "\n")
            (database / "config").write_text("[core]\nrepositoryformatversion = 0\nbare = false\n")
        (root / "NMRNetplusplus/.git").write_text("gitdir: ../.git/modules/nmrnet\n")
        for directory in ("tests", "repository_checks"):
            (root / directory).mkdir()
        (root / "bin").mkdir()
        for name, source in (("docker", "docker.py"), ("git", "git.sh")):
            (root / "bin" / name).symlink_to(FIXTURES / source)
        shutil.copyfile(ROOT / "tools/wheelhouse.sh", root / "tools/wheelhouse.sh")
        (root / ".work/wheelhouse/cpu").mkdir(parents=True)
        (root / ".work/wheelhouse/cpu/.complete").write_text("{}\n")
        (root / ".work/inference").mkdir()
        (root / ".work/inference/image-id").write_text("sha256:" + "b" * 64 + "\n")
        (root / ".work/checker").mkdir()
        (root / ".work/checker/image-id").write_text("sha256:" + "b" * 64 + "\n")
        (root / "requirements").mkdir()
        for name in ("inference.in", "build.in", "unicore-source.json"):
            (root / "requirements" / name).write_text("fixture\n")
        (root / "requirements/inference.lock").write_text("fixture==1\n")
        (root / "artifacts").mkdir()
        (root / "artifacts/nmrnet-source.paths").write_text("source_revision " + "a" * 40 + "\nLICENSE\nuninmr\n")
        manifest = "".join(
            hashlib.sha256((root / "NMRNetplusplus" / name).read_bytes()).hexdigest() + "  " + name + "\n"
            for name in ("LICENSE", "uninmr/infer.py")
        )
        (root / "artifacts/nmrnet-source.sha256").write_text(manifest)
        self.capture_committed_inputs()
        self.environment = os.environ | {
            "PATH": str(root / "bin") + ":" + str(Path(sys.executable).parent) + ":" + os.defpath,
            "PYTHONPATH": str(ROOT),
            "NMRNET_FAKE_ENGINE_LOG": str(self.log),
            # Ordinary shared completion belongs to this fixture; additions own faults.
            "NMRNET_FAKE_ENGINE_STATUS": "0",
            # Ordinary metadata belongs to this fixture; per-call additions
            # remain explicit fault stimuli, including malformed values.
            "NMRNET_FAKE_IMAGE_ID": "sha256:" + "b" * 64,
            "NMRNET_FAKE_IMAGE_REVISION": "a" * 40,
            # The constructor owns an ordinary worker result; additions own faults.
            "NMRNET_FAKE_WORKER_STATUS": "0",
            # State bytes and inspection status jointly describe the ordinary proxy.
            "NMRNET_FAKE_PROXY_STATE": "true",
            "NMRNET_FAKE_PROXY_INSPECT_STATUS": "0",
        }

    def capture_committed_inputs(self):
        # Store independent committed bytes/modes before a probe mutates the
        # worktree. The Git double can answer only these finite object records.
        for source, namespace, paths, tree_name, selection_name in (
            (
                self.root,
                "parent",
                (self.root / "containers/inference/image-inputs.txt").read_text().splitlines(),
                "tree",
                "selection.paths",
            ),
            (self.root / "NMRNetplusplus", "module", ["LICENSE", "uninmr/infer.py"], "tree", "selection.paths"),
            (
                self.root,
                "parent",
                [
                    "containers/packages/Dockerfile.lock",
                    "containers/packages/Dockerfile.lock.dockerignore",
                    "tools/container_guard.py",
                    "tools/package_lock.py",
                ],
                "package-stage.tree",
                "package-stage.paths",
            ),
            (
                self.root,
                "parent",
                ["tools/container_guard.py", "tools/package_lock.py"],
                "package-apply.tree",
                "package-apply.paths",
            ),
        ):
            database = self.root / ".fake-git" / namespace
            database.mkdir(parents=True, exist_ok=True)
            tree = bytearray()
            for name in paths:
                content = (source / name).read_bytes()
                object_id = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
                (database / object_id).write_bytes(content)
                mode = "100755" if (source / name).stat().st_mode & 0o111 else "100644"
                tree.extend((mode + " blob " + object_id + "\t" + name).encode() + b"\0")
            (database / tree_name).write_bytes(tree)
            (database / "HEAD").write_text("a" * 40 + "\n")
            selection = paths if namespace == "parent" else ["LICENSE", "uninmr"]
            (database / selection_name).write_text("\n".join(selection) + "\n")
        (self.root / ".fake-git/parent/gitlink").write_text("160000 commit " + "a" * 40 + "\tNMRNetplusplus\n")
        (self.root / ".fake-git/parent/index-gitlink").write_text("160000 " + "a" * 40 + " 0 NMRNetplusplus\n")
        shutil.copyfile(self.root / "containers/inference/image-inputs.txt", self.root / ".fake-git/parent/declared-inputs")

    def baseline_inputs(self):
        for name in ("baseline_prepare", "baseline_model", "baseline_output", "baseline_result", "baseline_run"):
            shutil.copyfile(ROOT / "tools" / (name + ".py"), self.root / "tools" / (name + ".py"))
        (self.root / "artifacts").mkdir(exist_ok=True)
        for name in ("release.json", "checkpoints.selection.json", "data.selection.json", "weight.selection.json"):
            shutil.copyfile(ROOT / "artifacts" / name, self.root / "artifacts" / name)
        for name in (".work/baseline/prepared", ".work/prepared/checkpoints", ".work/prepared/data", ".work/prepared/weight"):
            directory = self.root / name
            directory.mkdir(parents=True)
            (directory / "provenance.json").write_text("{}\n")
        return {
            "NMRNET_LABEL_ATOL_INPUT": "0",
            "NMRNET_ATOL_INPUT": "0",
            "NMRNET_RTOL_INPUT": "0",
            "NMRNET_RUN_ID_INPUT": "fixture-run",
        }

    def build_evidence(self):
        return json.loads((self.root / "build-evidence.json").read_text())

    @staticmethod
    def native_deadline_adapter(fixture, *, additions, budget):
        # This explicit CLI control owns the engine-client deadline. The fixture
        # timeout remains only a larger bound for cleaning up a failed probe.
        return fixture.run("inference_image.sh", "--timeout", str(budget), "import", additions=additions, timeout=3)

    def run(self, launcher, *arguments, additions=None, timeout=10, corrupt_worker_receipt=None):
        try:
            command = ["bash", str(self.root / "tools" / launcher), *arguments]
            process = subprocess.Popen(
                command,
                env=self.environment | (additions or {}),
                cwd=self.root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                if corrupt_worker_receipt is not None:
                    # Mutate only after the exact fixture attach client is
                    # running. This is a probe deadline, never operator policy.
                    ready = self.root / "attach-ready"
                    deadline = time.monotonic() + timeout
                    while not ready.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    if not ready.exists():
                        raise subprocess.TimeoutExpired(command, timeout)
                    receipt = Path((self.root / "worker-cid-path").read_text())
                    receipt.write_bytes(corrupt_worker_receipt)
                    process.send_signal(signal.SIGTERM)
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # A timed-out probe must not leave an engine double or a child
                # holding its output pipe. This group was created by this fixture.
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=2)
                raise
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        finally:
            for call in self.calls():
                values = call["arguments"]
                if values[:1] == ["create"] and "--cidfile" in values:
                    path = Path(values[values.index("--cidfile") + 1]).parent
                    if path.parent == Path("/tmp") and path.name.startswith("nmrnet-proxy."):
                        self.scratch.add(path)

    def make(self, *arguments, additions=None):
        environment = self.environment | (additions or {})
        if shutil.which("make", path=environment["PATH"]) is None:
            raise AssertionError(
                "Cannot prove literal GNU Make input handoff: the maintained contained image has no make executable."
            )
        # Revoke inherited GNU Make controls from this operation's fresh copy.
        # An absent control already has the desired state; remove present ones.
        for name in ("MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"):
            if name in environment:
                del environment[name]
        return subprocess.run(
            ["make", "--no-print-directory", "-C", str(self.root), *arguments],
            env=environment,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

    def calls(self):
        return read_engine_calls(self.log)

    def dispose(self):
        # Only acquisition scratch recorded by this fixture's create calls is
        # owned here; never glob /tmp or delete an unrelated retained operation.
        for scratch in self.scratch:
            shutil.rmtree(scratch, ignore_errors=True)
