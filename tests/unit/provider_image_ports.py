"""Small committed source scaffold; no engine or dependency installation authority."""

import os
import subprocess
import tempfile
from pathlib import Path

from tests.unit.wheel_capture_ports import run_wheel_capture
from tests.unit.wheel_pool_fixtures import locked, wheel


class ImageSource:
    def __enter__(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.files = [
            "Makefile",
            "tools/provider.sh",
            "tools/provider_logging.sh",
            "tools/docker_client.sh",
            "tools/inference_native.sh",
            "tools/worker_retirement.sh",
            "tools/geometry_image.sh",
            "tools/wheelhouse_path.sh",
            "tools/wheelhouse_inputs.sh",
            "containers/provider/Dockerfile",
            "containers/provider/Dockerfile.dockerignore",
            "requirements/provider-core-build.lock",
            "requirements/provider-core.lock",
            "packages/provider-core/pyproject.toml",
            "nmrnet_inference/predict.py",
            "nmrnet_inference/model.py",
            "nmrnet_inference/release.py",
            "nmrnet_inference/worker.py",
            "tools/artifacts.py",
            "tools/container_guard.py",
            "tools/provider_proxy.py",
            "tools/bound_http_proxy.py",
            "tools/container_resources.py",
            "tools/baseline_prepare.py",
            "tools/provider_workers.sh",
            "requirements/inference.lock",
            "requirements/unicore-source.json",
            "artifacts/prediction-models.json",
            "tools/provider_environment.py",
            "tools/wheelhouse.py",
            "tools/provider_image.sh",
            "tools/git_inputs.sh",
            "tools/git_metadata.sh",
            "nmrnet_provider/process.py",
            "nmrnet_provider/__init__.py",
            "packages/provider-core/src/provider_core/__init__.py",
            "nmrnet_provider/worker_main.py",
            "nmrnet_provider/geometry_worker.py",
            "nmrnet_provider/smiles_worker.py",
        ]
        for name in self.files:
            target = self.repository / name
            target.parent.mkdir(parents=True, exist_ok=True)
            body = (name + " authored fixture\n").encode()
            if name in (
                "tools/provider_image.sh",
                "tools/git_inputs.sh",
                "tools/git_metadata.sh",
                "tools/wheelhouse_path.sh",
                "tools/wheelhouse_inputs.sh",
                "tools/worker_retirement.sh",
                "tools/wheelhouse.py",
                "tools/artifacts.py",
                "tools/container_guard.py",
            ):
                body = Path(name).read_bytes()
            elif name == "requirements/provider-core-build.lock":
                body = b"setuptools==84.0.0 \\\n+    --hash=sha256:PLACEHOLDER\n"
            elif name == "requirements/provider-core.lock":
                body = b"provider-fixture==1.0 \\\n+    --hash=sha256:PLACEHOLDER\n"
            target.write_bytes(body)
        wheelhouse = self.repository / ".work/wheelhouse"
        wheelhouse.mkdir(parents=True)
        build_wheel = wheel(wheelhouse, "setuptools", "84.0.0")
        runtime_wheel = wheel(wheelhouse, "providerfixture", "1.0")
        alternate_runtime = wheel(wheelhouse, "providerfixture", "2.0")
        unrelated = wheel(wheelhouse, "unrelated-project", "9.0")
        locked(
            self.repository / "requirements/provider-core-build.lock",
            [("setuptools", "84.0.0", [build_wheel])],
        )
        locked(
            self.repository / "requirements/provider-core.lock",
            [("providerfixture", "1.0", [runtime_wheel])],
        )
        self.git("init", "--quiet")
        self.git("add", "--", *self.files)
        self.git("commit", "--quiet", "-m", "Authored image input fixture", "--", *self.files)
        self.pool_wheels = {
            "build": build_wheel,
            "runtime": runtime_wheel,
            "alternate_runtime": alternate_runtime,
            "unrelated": unrelated,
        }
        self.capture_count = 0
        self.wheelhouse = wheelhouse
        return self

    def __exit__(self, *_failure):
        self.scratch.cleanup()

    def git(self, *arguments):
        return subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=Image fixture",
                "-c",
                "user.email=fixture@example.invalid",
                *arguments,
            ],
            cwd=self.repository,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def capture(self):
        self.capture_count += 1
        capture_directory = self.root / str(self.capture_count)
        capture_directory.mkdir(mode=0o700)
        result = subprocess.run(
            [
                "bash",
                "-eu",
                "-c",
                'repository_root=$1; image_input_directory=$2; wheelhouse_directory=$3; source "$repository_root/tools/provider_image.sh"; capture_provider_inputs',
                "image-capture",
                str(self.repository),
                str(capture_directory),
                str(self.wheelhouse),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            for lock, wheel_destination in (
                ("provider-core.lock", "runtime-wheels"),
                ("provider-core-build.lock", "build-wheels"),
            ):
                arguments = self.wheel_capture_arguments(capture_directory, wheel_destination, lock)
                status = run_wheel_capture(arguments)
                if status != 0:
                    result = subprocess.CompletedProcess(arguments, status, "", f"Frozen wheel capture failed for {lock}.\n")
                    break
        return result, capture_directory

    def wheel_capture_arguments(self, capture_directory, wheel_destination, lock):
        script = r"""
repository_root=$1
image_input_directory=$2
wheelhouse_directory=$3
wheel_destination=$4
lock=$5
caller_uid=$(id -u)
caller_gid=$(id -g)
engine=(fixture)
source "$repository_root/tools/wheelhouse_inputs.sh"
prepare_wheel_capture "$image_input_directory/context" provider-core "$lock" \
    "$image_input_directory/$wheel_destination" PINNED_IMAGE
printf '%s\0' "${wheel_capture[@]}"
"""
        output = subprocess.run(
            [
                "bash",
                "-eu",
                "-c",
                script,
                "wheel-capture",
                str(self.repository),
                str(capture_directory),
                str(self.wheelhouse),
                wheel_destination,
                lock,
            ],
            capture_output=True,
            check=True,
        ).stdout
        return [part.decode() for part in output.split(b"\0") if part]
