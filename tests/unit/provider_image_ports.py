"""Small committed source scaffold; no engine or dependency installation authority."""

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path

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
            "tools/operator_guidance.sh",
            "containers/provider/Dockerfile",
            "containers/provider/Dockerfile.dockerignore",
            "requirements/provider-core-install.lock",
            "host/bootstrap/acquisition/acquisition_tool/__init__.py",
            "host/bootstrap/acquisition/acquisition_tool/installer/requirements/installer-release.json",
            "host/bootstrap/acquisition/acquisition_tool/installer/tools/container_guard.py",
            "host/bootstrap/acquisition/acquisition_tool/installer/tools/installer.py",
            "host/bootstrap/acquisition/acquisition_tool/installer/wheels/pip-26.2.2-py3-none-any.whl",
            "requirements/wheelhouse-tool-assets.in",
            "tools/streamed_build.sh",
            "host/git_stream_sources.py",
            "nmrnet_provider/about/hello.md",
            "nmrnet_inference/predict.py",
            "nmrnet_inference/model.py",
            "nmrnet_inference/release.py",
            "nmrnet_inference/worker.py",
            "tools/artifacts.py",
            "tools/container_guard.py",
            "tools/container_resources.py",
            "tools/baseline_prepare.py",
            "requirements/inference.lock",
            "requirements/unicore-source.json",
            "artifacts/prediction-models.json",
            "tools/provider_environment.py",
            "tools/provider_deployment.py",
            "tools/wheelhouse.py",
            "tools/provider_image.sh",
            "tools/core_image.sh",
            "tools/git_inputs.sh",
            "tools/git_metadata.sh",
            "nmrnet_provider/process.py",
            "nmrnet_provider/__init__.py",
            "nmrnet_provider/worker_main.py",
            "nmrnet_provider/native_child.py",
            "nmrnet_provider/geometry_worker.py",
            "nmrnet_provider/smiles_worker.py",
        ]
        for name in self.files:
            target = self.repository / name
            target.parent.mkdir(parents=True, exist_ok=True)
            body = (name + " authored fixture\n").encode()
            if name in (
                "tools/provider_image.sh",
                "tools/core_image.sh",
                "tools/git_inputs.sh",
                "tools/git_metadata.sh",
                "tools/wheelhouse_path.sh",
                "tools/operator_guidance.sh",
                "tools/worker_retirement.sh",
                "tools/wheelhouse.py",
                "tools/artifacts.py",
                "tools/container_guard.py",
                "tools/streamed_build.sh",
                "host/git_stream_sources.py",
            ):
                body = Path(name).read_bytes()
            elif name == "requirements/provider-core-install.lock":
                body = b"provider-fixture==1.0 \\\n+    --hash=sha256:PLACEHOLDER\n"
            target.write_bytes(body)
        wheelhouse = self.repository / ".work/wheelhouse"
        wheelhouse.mkdir(parents=True)
        runtime_wheel = wheel(wheelhouse, "providerfixture", "1.0")
        alternate_runtime = wheel(wheelhouse, "providerfixture", "2.0")
        unrelated = wheel(wheelhouse, "unrelated-project", "9.0")
        streamer = wheelhouse / "wheelhouse-tool-1.0.0.pyz"
        streamer.write_text("""import hashlib, json, pathlib, sys, tarfile
from urllib.parse import urlsplit
args = sys.argv
if args[1] == "report":
    report = json.loads(pathlib.Path(args[args.index("--report") + 1]).read_text())
    directory = args[args.index("--directory") + 1]
    items = [{"directory": directory, "filename": pathlib.Path(urlsplit(item["download_info"]["url"]).path).name,
              "sha256": item["download_info"]["archive_info"]["hashes"]["sha256"]} for item in report["install"]]
    print(json.dumps(items))
elif args[1] == "identity":
    items = json.loads(pathlib.Path(args[args.index("--manifest") + 1]).read_text())
    print(hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest())
elif args[1] == "stream":
    sources = json.loads(pathlib.Path(args[args.index("--source-allowlist") + 1]).read_text())
    items = json.loads(pathlib.Path(args[args.index("--manifest") + 1]).read_text())
    pool = pathlib.Path(args[args.index("--pool") + 1])
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
        for item in sources:
            tar.add(item["path"], arcname=item["destination"])
        for item in items:
            tar.add(pool / item["filename"], arcname=item["directory"] + "/" + item["filename"])
""")
        pin = hashlib.sha256(streamer.read_bytes()).hexdigest()
        (self.repository / "requirements/wheelhouse-tool-assets.in").write_text(
            f"https://github.com/numpde/nmr-provider-tools/releases/download/v1/{streamer.name}#sha256={pin}\n"
        )
        locked(
            self.repository / "requirements/provider-core-install.lock",
            [("providerfixture", "1.0", [runtime_wheel])],
        )
        self.git("init", "--quiet")
        self.git("add", "--", *self.files)
        self.git("commit", "--quiet", "-m", "Authored image input fixture", "--", *self.files)
        self.pool_wheels = {
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

    def capture(self, *, kind="provider"):
        self.capture_count += 1
        capture_directory = self.root / str(self.capture_count)
        capture_directory.mkdir(mode=0o700)
        result = subprocess.run(
            [
                "bash",
                "-eu",
                "-c",
                'repository_root=$1; image_input_directory=$2; wheelhouse_directory=$3; image_kind=$4; source "$repository_root/tools/${image_kind}_image.sh"; "capture_${image_kind}_inputs"',
                "image-capture",
                str(self.repository),
                str(capture_directory),
                str(self.wheelhouse),
                kind,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return result, capture_directory
