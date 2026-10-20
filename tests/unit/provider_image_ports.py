"""Small committed source scaffold; no engine or dependency installation authority."""

import os
import subprocess
import tempfile
from pathlib import Path


class ImageSource:
    def __enter__(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.files = [
            "Makefile",
            "tools/provider.sh",
            "tools/docker_client.sh",
            "tools/inference_native.sh",
            "tools/worker_retirement.sh",
            "tools/geometry_image.sh",
            "containers/provider/Dockerfile",
            "containers/provider/Dockerfile.dockerignore",
            "requirements/provider-core-build.lock",
            "requirements/provider-core.lock",
            "packages/provider-core/README.md",
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
            if name in ("tools/provider_image.sh", "tools/git_inputs.sh", "tools/git_metadata.sh"):
                body = Path(name).read_bytes()
            target.write_bytes(body)
        self.git("init", "--quiet")
        self.git("add", "--", *self.files)
        self.git("commit", "--quiet", "-m", "Authored image input fixture", "--", *self.files)
        for name in (
            ".work/provider-core/wheelhouse/.complete",
            ".work/provider-core/wheelhouse/example.whl",
            ".work/wheelhouse/cpu/setuptools-84.0.0-py3-none-any.whl",
        ):
            target = self.repository / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"capture stimulus, not an installable dependency")
        self.capture_count = 0
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
        destination = self.root / str(self.capture_count)
        destination.mkdir(mode=0o700)
        result = subprocess.run(
            [
                "bash",
                "-eu",
                "-c",
                'repository_root=$1; image_input_directory=$2; source "$repository_root/tools/provider_image.sh"; capture_provider_inputs',
                "image-capture",
                str(self.repository),
                str(destination),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result, destination
