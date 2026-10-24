"""Freeze API-only shared delivery inputs; never call an Engine or run science."""

from tools.container_guard import require_container

require_container()

import argparse
import copy
import json
import os
import re
import stat
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from nmrnet_provider.deployment import _bind, _service, compose_bytes
from nmrnet_provider.process_reports import _owned_relationships, _safe_owner_details
from provider_core.api_config import api_settings
from provider_core.attempt_record import RecoveryScope
from provider_core.delivery_client import delivery_volume_name
from provider_core.diagnostics import failure_evidence
from provider_core.response_json import read_document
from provider_core.producer_checkpoint import checkpoint_volume_name
from provider_core.startup_file import read_startup_file
from tools.artifacts import fsync_directory
from tools.provider_proxy import ServicePolicy

PROJECT = "nmr-api-delivery"
CORE_PYTHON = "/opt/provider-core/environment/bin/python"


class DeliveryDeploymentRejected(ValueError):
    """Code-owned plan/grant refusal, safe for bounded protected diagnostics."""


def _details(error, _text, _window):
    return {"message": str(error)} if type(error) is DeliveryDeploymentRejected else _safe_owner_details(error)


def _path(value):
    path = Path(value)
    if not path.is_absolute() or path.resolve() != path or any(c in str(path) for c in ("\0", "\n", "\r", ",")):
        raise DeliveryDeploymentRejected("Delivery inputs require canonical absolute paths without mount delimiters.")
    return path


def _write(path, content, mode=0o600):
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _volume(name, target, readonly=False):
    return {"type": "volume", "source": name, "target": target, "read_only": readonly,
            "volume": {"nocopy": True}}


def _scalar(value):
    return json.dumps(value, allow_nan=False)


def _api_proxy(original, plan, settings, *, key, image, uid, gid, labels, output, binding_directory, source_bundle, project):
    proxy = copy.deepcopy(original)
    command = proxy.get("command")
    allowed = {"--mode", "--interface", "--socket", "--ready-file", "--status-directory", "--authority", "--address"}
    if (type(command) is not list or len(command) % 2 or any(type(v) is not str for v in command)
            or any(command[i] not in allowed for i in range(0, len(command), 2))):
        raise DeliveryDeploymentRejected("The installed API proxy command is unsupported; preserve its original bundle.")
    options = {}
    for i in range(0, len(command), 2):
        options.setdefault(command[i], []).append(command[i + 1])
    for name in ("--mode", "--interface", "--socket", "--ready-file", "--status-directory"):
        if len(options.get(name, ())) != 1:
            raise DeliveryDeploymentRejected("The installed API proxy must retain exactly one explicit routing policy.")
    parsed = urlsplit(settings.identity.origin)
    authorities = [f"{parsed.hostname}:{parsed.port or 443}"]
    if options.get("--authority") != authorities:
        raise DeliveryDeploymentRejected("The installed API proxy authority differs from the verified delivery API origin.")
    mode = options["--mode"][0]
    ServicePolicy.admit(mode, authorities, options.get("--address", []))
    if (options["--socket"] != ["/route/proxy.sock"] or options["--ready-file"] != ["/route/ready"]
            or options["--status-directory"] != ["/run"]):
        raise DeliveryDeploymentRejected("The installed API proxy must use its fixed private runtime paths.")
    route_root = output / "runtime" / key
    route_root.mkdir(mode=0o700)
    proxy.update(image=image, entrypoint=[CORE_PYTHON, "-I", "-B", "-m", "tools.provider_proxy"],
                 user=f"{uid}:{gid}", labels=labels, container_name=project + "-api-" + key[:16],
                 volumes=[_bind(route_root, "/route", readonly=False)])
    # Proxy modules and their status checker share the installed core environment.
    proxy["healthcheck"] = {"test": ["CMD", CORE_PYTHON, "-I", "-B", "-m", "provider_core.process_status",
                                      "/run/status.json", "proxy"],
                            "interval": "5s", "timeout": "2s", "retries": 3, "start_period": "5s"}
    networks = {}
    if mode == "local":
        attached = proxy.get("networks")
        if type(attached) is not list or len(attached) != 1:
            raise DeliveryDeploymentRejected("The installed local API proxy must have one exact external network.")
        network = plan.get("networks", {}).get(attached[0])
        if (type(network) is not dict or network.get("external") is not True
                or re.fullmatch(r"[0-9a-f]{64}", network.get("name", "")) is None):
            raise DeliveryDeploymentRejected("The installed API network must retain its full immutable Engine identity.")
        network_key = "api-" + key[:16]
        proxy.pop("network_mode", None)
        proxy["networks"] = [network_key]
        networks[network_key] = network
    else:
        if proxy.get("network_mode") != "host" or "networks" in proxy:
            raise DeliveryDeploymentRejected("The installed mapped/external API proxy must retain its original host network mode.")
    original_mounts = original.get("volumes", [])
    expected_targets = {"/route", "/etc/resolv.conf"} if mode == "external" else {"/route"}
    if (type(original_mounts) is not list or len(original_mounts) != len(expected_targets)
            or {mount.get("target") for mount in original_mounts} != expected_targets
            or any(mount.get("type") != "bind" for mount in original_mounts)):
        raise DeliveryDeploymentRejected("The installed API proxy has an unexpected filesystem grant.")
    if mode == "external":
        resolver = next(mount for mount in original_mounts if mount["target"] == "/etc/resolv.conf")
        if resolver.get("read_only") is not True:
            raise DeliveryDeploymentRejected("API resolver configuration must be an original read-only grant.")
        if _path(resolver["source"]) != source_bundle / "resolv.conf":
            raise DeliveryDeploymentRejected("API resolver configuration must remain inside its original sealed bundle.")
        raw = read_startup_file(resolver["source"], maximum_bytes=65536, label="installed API resolver")
        _write(binding_directory / "resolv.conf", raw)
        proxy["volumes"].append(_bind(binding_directory / "resolv.conf", "/etc/resolv.conf"))
    return proxy, networks, route_root


# silent-defaults: allow-contract -- Production keeps the existing host singleton; only the contained qualification caller selects a distinct literal project.
def prepare(provider_bundles, output, uid, gid, delivery_image, controller_image, *, project=PROJECT):
    if type(project) is not str or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project) is None:
        raise DeliveryDeploymentRejected("Delivery project requires one explicit lowercase Compose project name of up to 63 characters.")
    queue = project + "-queue"
    if (type(uid) is not int or type(gid) is not int or uid <= 0 or gid <= 0 or uid != os.geteuid()
            or any(re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None for image in (delivery_image, controller_image))):
        raise DeliveryDeploymentRejected("Delivery freezing requires its installing nonzero UID/GID and two immutable image IDs.")
    output = _path(output)
    bundles = tuple(_path(bundle) for bundle in provider_bundles)
    if not 1 <= len(bundles) <= 16 or len(set(bundles)) != len(bundles):
        raise DeliveryDeploymentRejected("Delivery requires one through sixteen distinct installed provider bundles.")
    if any(output.is_relative_to(bundle) or bundle.is_relative_to(output) for bundle in bundles):
        raise DeliveryDeploymentRejected("Delivery output must be disjoint from all original provider bundles.")
    facts = output.lstat()
    if (not stat.S_ISDIR(facts.st_mode) or facts.st_uid != uid or stat.S_IMODE(facts.st_mode) != 0o700
            or any(output.iterdir())):
        raise DeliveryDeploymentRejected("Delivery output requires a precreated empty caller-owned mode 0700 directory.")
    (output / "bindings").mkdir(mode=0o700)
    (output / "runtime").mkdir(mode=0o700)
    services, networks, volumes, bindings, roots, seen = {}, {}, {}, [], [], set()
    volumes[queue] = {"name": queue}
    roots.append({"volume": queue, "target": "/volumes/queue", "mode": 0o700})
    for bundle in bundles:
        facts = bundle.lstat()
        if not stat.S_ISDIR(facts.st_mode) or facts.st_uid != uid or stat.S_IMODE(facts.st_mode) != 0o500:
            raise DeliveryDeploymentRejected("Delivery consumes only original caller-owned sealed mode 0500 provider bundles.")
        document = tomllib.loads(read_startup_file(bundle / "config/provider.toml", maximum_bytes=65536,
                                                  label="installed provider API configuration", secret=True).decode("utf-8"))
        table = document["api"]
        consumed = {}
        def read_original(value, **limits):
            path = _path(value)
            if path.parent != bundle / "secrets":
                raise DeliveryDeploymentRejected("Installed API credential and CA must remain in their original sealed secret leaf.")
            if path not in consumed:
                consumed[path] = read_startup_file(path, **limits)
            if len(consumed[path]) > limits["maximum_bytes"]:
                raise DeliveryDeploymentRejected("The admitted API input exceeds its required byte bound.")
            return consumed[path]
        settings, provider_ref = api_settings(table, read_original)
        identity = settings.identity.origin, provider_ref
        if identity in seen:
            raise DeliveryDeploymentRejected("Two selected provider bundles claim the same delivery API/provider identity.")
        seen.add(identity)
        ipc_volume = delivery_volume_name(*identity, namespace=project)
        key = ipc_volume.removeprefix(project + "-ipc-")
        if re.fullmatch(r"[0-9a-f]{64}", key) is None:
            raise DeliveryDeploymentRejected("The shared delivery volume owner returned an unsupported binding identity.")
        directory = output / "bindings" / key
        directory.mkdir(mode=0o700)
        api = dict(table)
        _write(directory / "signing.json", read_original(table["credential_file"], maximum_bytes=16384,
                                                         label="installed API signing credential", secret=True))
        api["credential_file"] = f"/input/bindings/{key}/signing.json"
        if "ca_file" in table:
            _write(directory / "ca.pem", read_original(table["ca_file"], maximum_bytes=1048576, label="installed API CA"))
            api["ca_file"] = f"/input/bindings/{key}/ca.pem"
        plan = read_document(read_startup_file(bundle / "compose.json", maximum_bytes=1048576,
                                               label="installed provider Compose recipe", secret=True).replace(b"$$", b"$"), 1048576)
        labels = {"io.nmrnet-repro.delivery": project, "io.nmrnet-repro.delivery-binding": key}
        proxy, attached, route_root = _api_proxy(plan["services"]["api"], plan, settings, key=key,
            image=delivery_image, uid=uid, gid=gid, labels=labels, output=output,
            binding_directory=directory, source_bundle=bundle, project=project)
        role = "api-" + key[:16]
        if role in services:
            raise DeliveryDeploymentRejected("Delivery proxy service identities collide; no ambiguous recipe was published.")
        services[role] = proxy
        networks.update(attached)
        volumes[ipc_volume] = {"name": ipc_volume}
        roots.append({"volume": ipc_volume, "target": f"/volumes/ipc-{key}", "mode": 0o710})
        lifecycle = document["lifecycle"]
        control = checkpoint_volume_name(RecoveryScope(*identity, lifecycle["consumer_id"], lifecycle["generation_id"]), namespace=project)
        volumes[control] = {"name": control}
        roots.append({"volume": control, "target": "/volumes/control-" + control.removeprefix(project + "-control-"), "mode": 0o700})
        socket_leaf = Path(document["delivery"]["socket_path"]).name
        bindings.append((key, ipc_volume, route_root, api, socket_leaf))
    daemon = _service(delivery_image, CORE_PYTHON, "provider_core.delivery_main", uid=uid, gid=gid,
                      labels={"io.nmrnet-repro.delivery": project}, stop_seconds=60)
    daemon["container_name"] = project + "-daemon"
    daemon["command"] = ["--config", "/input/daemon.toml"]
    daemon["volumes"] = [_bind(output, "/input"), _volume(queue, "/state")]
    text = ['schema_id = "provider-core.delivery.v1"', 'state_directory = "/state"',
            "request_seconds = 30", "retry_initial_seconds = 1", "retry_max_seconds = 30", "[limits]",
            "provider_items = 8", "provider_bytes = 16777216", f"total_items = {8 * len(bindings)}",
            f"total_bytes = {16777216 * len(bindings)}"]
    for key, ipc_volume, route_root, api, socket_leaf in bindings:
        daemon["volumes"] += [_volume(ipc_volume, f"/ipc/{key}"), _bind(route_root, f"/routes/{key}")]
        text += ["[[bindings]]", f"socket_path = {_scalar('/ipc/' + key + '/' + socket_leaf)}", f"socket_group_id = {gid}",
                 f'proxy_socket = "/routes/{key}/proxy.sock"', "[bindings.api]"]
        text += [f"{name} = {_scalar(value)}" for name, value in api.items()]
    services["delivery"] = daemon
    initializer = _service(controller_image, "/opt/provider/environment/bin/python",
        "nmrnet_provider.delivery_deployment", uid=0, gid=0,
        labels={"io.nmrnet-repro.delivery": project}, stop_seconds=10)
    initialize_command = ["initialize", "--uid", str(uid), "--gid", str(gid)]
    for root in roots:
        kind = "queue" if root["volume"] == queue else "ipc" if root["mode"] == 0o710 else "control"
        initialize_command += ["--" + kind, root["target"]]
    initializer.update(container_name=project + "-init", restart="no", cap_add=["CHOWN"],
        command=initialize_command, volumes=[_volume(root["volume"], root["target"]) for root in roots])
    services["init"] = initializer
    daemon["depends_on"] = {"init": {"condition": "service_completed_successfully"}}
    _write(output / "daemon.toml", ("\n".join(text) + "\n").encode())
    _write(output / "compose.json", compose_bytes({"name": project, "services": services, "networks": networks, "volumes": volumes}))
    for directory in (output / "bindings", *(output / "bindings").iterdir(), output / "runtime", output):
        fsync_directory(directory)


def initialize(uid, gid, queue, ipc, control):
    if os.geteuid() != 0 or uid <= 0 or gid <= 0:
        raise DeliveryDeploymentRejected("Volume initialization requires root with CHOWN and explicit nonzero target UID/GID.")
    for target, mode in [(queue, 0o700), *((path, 0o710) for path in ipc), *((path, 0o700) for path in control)]:
        path = _path(target)
        observed = path.lstat()
        if not stat.S_ISDIR(observed.st_mode):
            raise DeliveryDeploymentRejected("Delivery volume root must be a real directory.")
        if (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode)) == (uid, gid, mode):
            continue
        if observed.st_uid != 0 or any(path.iterdir()):
            raise DeliveryDeploymentRejected("Refusing to change an existing delivery volume with unexpected ownership, mode or contents.")
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fchmod(descriptor, mode)
            os.fchown(descriptor, uid, gid)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--provider-bundle", type=Path, action="append", required=True)
    prepare_parser.add_argument("--output", type=Path, required=True)
    prepare_parser.add_argument("--project", default=PROJECT)
    for name in ("uid", "gid"):
        prepare_parser.add_argument("--" + name, type=int, required=True)
    for name in ("delivery-image", "controller-image"):
        prepare_parser.add_argument("--" + name, required=True)
    initializer = commands.add_parser("initialize")
    for name in ("uid", "gid"):
        initializer.add_argument("--" + name, type=int, required=True)
    initializer.add_argument("--queue", type=Path, required=True)
    initializer.add_argument("--ipc", type=Path, action="append", required=True)
    initializer.add_argument("--control", type=Path, action="append", required=True)
    options = parser.parse_args()
    if options.command == "prepare":
        prepare(options.provider_bundle, options.output, options.uid, options.gid, options.delivery_image, options.controller_image, project=options.project)
    else:
        initialize(options.uid, options.gid, options.queue, options.ipc, options.control)


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        sys.stderr.write(json.dumps(failure_evidence(error, secrets=(), describe_owner=_details,
            owner_relationships=_owned_relationships), separators=(",", ":")) + "\n")
        raise SystemExit(2) from None
