"""Disposable caller facts and admitted configuration for fixed Compose plans."""

import tempfile
import hashlib
import json
from pathlib import Path

from nmrnet_provider.config import load_provider_config
from nmrnet_provider.deployment import render_compose
from tests.integration.nmrnet.config_ports import ConfigFiles, native_capture_files


class ComposeFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "secrets/deployments/web/bundles/generation-first"
        self.bundle.mkdir(parents=True, mode=0o700)
        self.files = ConfigFiles(self.bundle)
        raw = self.files.raw.replace(str(self.files.state).encode(), str(self.root / "attempts").encode())
        self.files.write(raw.replace(b"prediction_seconds = 90", b"prediction_seconds = 5"))
        self.config = load_provider_config(self.files.path)
        self.execution = {
            "image_id": "sha256:" + "1" * 64, "geometry_image_id": "sha256:" + "2" * 64,
            "controller_image_id": "sha256:" + "3" * 64, "repository_revision": "a" * 40,
            "upstream_revision": "b" * 40, "runtime_identity_source": "caller",
            "tool_sources": {"tools/worker.py": {"sha256": "c" * 64, "size": 1}},
        }
        self.routes = {role: {"mode": "external", "interface": "eth0"} for role in ("api", "interpreter", "store")}

    def plan(self, **changes):
        arguments = dict(deployment="web", checkout=self.root / "checkout", bundle=self.bundle,
                         uid=1000, gid=1001, prepared=self.root / "prepared", execution=self.execution,
                         routes=self.routes)
        arguments.update(changes)
        return render_compose(self.config, **arguments)


def deployment_inputs(root):
    """Author one offline deployment with fake credentials and source observations."""
    private = root / "secrets/deployments/web"
    for directory in (root / "secrets", root / "secrets/deployments", private, private / "inputs", private / "bundles"):
        directory.mkdir(mode=0o700)
    files = ConfigFiles(private / "inputs")
    definition = root / "provider.toml"
    definition.write_bytes(files.raw.replace(str(files.state).encode(), b"/srv/nmrnet/attempts").replace(
        str(files.root).encode(), b"/srv/nmrnet/input"))
    settings = private / "deployment.toml"
    settings.write_text("store_root = " + json.dumps(str(root / "attempts")) + '\nmaximum_startup_bytes = 65536\nresolvers = ["192.0.2.53"]\n'
                        + "".join(f'[routes.{role}]\nmode = "external"\ninterface = "eth0"\n' for role in ("api", "interpreter", "store")))
    settings.chmod(0o600)
    native = native_capture_files(root)
    source = native / "sources/requirements/unicore-source.json"
    source.write_bytes(b'{"name":"fixed-source-fixture"}\n')
    names = sorted((native / "inputs.paths").read_text().splitlines() + ["requirements/unicore-source.json"])
    (native / "inputs.paths").write_text("\n".join(names) + "\n")
    (native / "sources.sha256").write_text("".join(
        hashlib.sha256((native / "sources" / name).read_bytes()).hexdigest() + "  " + name + "\n" for name in names))
    member = {"member": "member.bin", "size": 1, "sha256": "c" * 64}
    models = root / "prediction-models.json"
    models.write_text(json.dumps({"dictionary": {**member, "archive_sha256": "d" * 64},
                                  "checkpoint_archive": {"archive_sha256": "e" * 64},
                                  "elements": {element: {role: member for role in ("checkpoint", "scaler")} for element in ("H", "C")}}))
    bundle, output = private / "bundles/generation-first", root / "output"
    bundle.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    return dict(definition=definition, settings=settings, secrets=files.root, bundle=bundle, output=output,
                native_capture=native, models=models, deployment="web", checkout=root / "checkout", uid=1000, gid=1001,
                prepared=root / "prepared", image_id="sha256:" + "1" * 64,
                geometry_image_id="sha256:" + "2" * 64, controller_image_id="sha256:" + "3" * 64,
                repository_revision="a" * 40, upstream_revision="b" * 40), files
