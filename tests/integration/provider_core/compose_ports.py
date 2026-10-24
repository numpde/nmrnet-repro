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

    def container_record(self, *, role="provider", identity="1" * 64, bundle="generation-first", running=False):
        return {"Id": identity, "Config": {"Labels": {
            "com.docker.compose.project": self.plan()["name"], "com.docker.compose.service": role,
            "com.docker.compose.oneoff": "False", "com.docker.compose.project.working_dir": str(self.root / "checkout"),
            "io.nmrnet-repro.checkout": str(self.root / "checkout"), "io.nmrnet-repro.deployment": "web",
            "io.nmrnet-repro.bundle": bundle,
        }}, "State": {"Running": running, "Restarting": False, "Pid": 123 if running else 0}}

    def grant_record(self, *, role="provider"):
        """Author the Engine inspect vocabulary for the fixed provider/reader grants.

        This is an admission stand-in, not observed Engine or scientific evidence.
        The stop timeout follows the one plan owner; creation-grant fields below
        are independently spelled in Docker's inspect vocabulary.
        """
        service = self.plan()["services"][role]
        record = self.container_record(role=role)
        image = {"Id": "sha256:" + ("3" if role == "provider" else "2") * 64,
                 "Config": {"Env": ["PATH=/image/bin", "LANG=C.UTF-8"], "WorkingDir": "/image/work", "StopSignal": ""}}
        runtime = self.bundle.parent.parent / "runtime"
        if role == "provider":
            command = ["--config", str(self.bundle / "config/provider.toml"), "--execution", "/input/execution.json",
                       "--models", "/input/prediction-models.json", "--diagnostics", "/run",
                       "--api-proxy-socket", "/routes/api/proxy.sock", "--interpreter-proxy-socket", "/routes/interpreter/proxy.sock",
                       "--store-proxy-socket", "/routes/store/proxy.sock"]
            python, module, temporary = "/opt/provider/environment/bin/python", "nmrnet_provider.process", "/run"
            binds = [(self.bundle, str(self.bundle), False),
                     (self.bundle / "execution.json", "/input/execution.json", False),
                     (self.bundle / "prediction-models.json", "/input/prediction-models.json", False),
                     (self.root / "attempts", str(self.root / "attempts"), True)]
            binds += [(runtime / name, "/native/" + name, False) for name in ("prediction", "reader", "smiles")]
            binds += [(runtime / ("route-" + name), "/routes/" + name, False) for name in ("api", "interpreter", "store")]
        else:
            command = ["reader", "--socket", "/native/reader/runner.sock", "--scratch", "/scratch",
                       "--io-seconds", "10", "--operation-seconds", "40", "--startup-seconds", "6.0",
                       "--terminate-seconds", "6.0", "--kill-seconds", "6.0", "--cleanup-seconds", "6.0"]
            python, module, temporary = "/opt/rdkit/environment/bin/python", "nmrnet_provider.worker_main", "/scratch"
            binds = [(runtime / "reader", "/native/reader", True)]
        record.update(Name="/" + service["container_name"], Image=image["Id"], Mounts=[{
            "Type": "bind", "Source": str(source), "Destination": target, "RW": writable,
            "Mode": "rw" if writable else "ro", "Propagation": "rprivate",
        } for source, target, writable in binds], NetworkSettings={"Networks": {"none": {"NetworkID": "0" * 64}}})
        record["Config"].update(Image=image["Id"], User="1000:1001", Entrypoint=[python, "-I", "-B", "-m", module],
                                Cmd=command, Env=[*image["Config"]["Env"], "NMRNET_LOG_LEVEL=INFO"], WorkingDir="/image/work",
                                StopTimeout=int(service["stop_grace_period"][:-1]), StopSignal="", OpenStdin=False, Tty=False)
        record["Config"]["Healthcheck"] = {"Test": service["healthcheck"]["test"], "Interval": 5_000_000_000,
                                           "Timeout": 2_000_000_000, "Retries": 3,
                                           "StartPeriod": int(service["healthcheck"]["start_period"][:-1]) * 1_000_000_000}
        record["HostConfig"] = {
            "ReadonlyRootfs": True, "Privileged": False, "Init": True, "CapDrop": ["ALL"], "CapAdd": None,
            "SecurityOpt": ["no-new-privileges:true"], "Memory": 536870912, "MemorySwap": 536870912,
            "PidsLimit": 64, "NanoCpus": 2000000000, "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "10m", "max-file": "3", "mode": "non-blocking", "max-buffer-size": "1m"}},
            "NetworkMode": "none", "PidMode": "", "UTSMode": "", "UsernsMode": "", "IpcMode": "private", "CgroupnsMode": "private",
            "Devices": [], "DeviceRequests": None, "DeviceCgroupRules": None, "ExtraHosts": None,
            "Dns": [], "DnsOptions": [], "DnsSearch": [], "Links": None, "VolumesFrom": None,
            "PortBindings": {}, "PublishAllPorts": False,
            "Sysctls": None,
            "MaskedPaths": ["/proc/acpi", "/proc/asound", "/proc/interrupts", "/proc/kcore", "/proc/keys", "/proc/latency_stats",
                            "/proc/sched_debug", "/proc/scsi", "/proc/timer_list", "/proc/timer_stats", "/sys/devices/virtual/powercap", "/sys/firmware"],
            "ReadonlyPaths": ["/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"],
            "Tmpfs": {target: "rw,nosuid,nodev,noexec,size=64m,mode=0700,uid=1000,gid=1001" for target in ("/tmp", "/run", temporary)},
        }
        return record, image


def deployment_inputs(root):
    """Author one offline deployment with fake credentials and source observations."""
    private = root / "secrets/deployments/web"
    for directory in (root / "secrets", root / "secrets/deployments", private, private / "inputs", private / "bundles"):
        directory.mkdir(mode=0o700)
    files = ConfigFiles(private / "inputs")
    definition = root / "provider.toml"
    definition.write_bytes(files.raw.replace(str(files.state).encode(), b"/srv/nmrnet/attempts").replace(
        str(files.root).encode(), b"/srv/nmrnet/input"))
    settings = root / "config/deployments/web/deployment.toml"
    settings.parent.mkdir(parents=True, mode=0o700)
    settings.write_text("store_root = " + json.dumps(str(root / "attempts")) + '\nmaximum_startup_bytes = 65536\nresolvers = ["192.0.2.53"]\n'
                        + "".join(f'[routes.{role}]\nmode = "external"\ninterface = "eth0"\n' for role in ("api", "interpreter", "store")))
    settings.chmod(0o600)
    native = native_capture_files(root)
    source = native / "sources/requirements/unicore-source.json"
    source.write_bytes(b'{"name":"fixed-source-fixture"}\n')
    (native / "sources/tools/provider_deployment.py").write_bytes(b"# Captured helper stand-in; never executed by this contract fixture.\n")
    names = sorted((native / "inputs.paths").read_text().splitlines() + ["requirements/unicore-source.json", "tools/provider_deployment.py"])
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
    return dict(definition=definition, settings=settings, secrets=files.root, bundle=bundle,
                native_capture=native, models=models, deployment="web", checkout=root / "checkout", uid=1000, gid=1001,
                prepared=root / "prepared", image_id="sha256:" + "1" * 64,
                geometry_image_id="sha256:" + "2" * 64, controller_image_id="sha256:" + "3" * 64,
                repository_revision="a" * 40, upstream_revision="b" * 40), files
