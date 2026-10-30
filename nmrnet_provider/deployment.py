"""Compose NMRNet's fixed runtime grants from admitted deployment inputs.

Contract: docs/notes/021_compose_runner_architecture_20261023.html. This contained
plan owner is not an Engine client. Existing
configuration and proxy owners admit credentials, scientific policy and exact
destinations. The host separately observes images, networks and mounted paths;
these caller-supplied facts do not become runtime attestations here.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import math
import re
from pathlib import Path

from nmrnet_provider.config import ConfigurationRejected
from provider_core.producer_checkpoint import checkpoint_volume_name
from nmrnet_provider.process import _declared_execution
from nmrnet_provider.startup_capture import _bind_path, startup_authorities, prediction_lifetime_seconds
from provider_core.provider_proxy import ServicePolicy, ServiceRefused
from provider_core.bound_http_proxy import require_interface_name


def project_name(checkout, deployment):
    """Keep project discovery stable across immutable bundle generations."""
    return "nmrnet-" + hashlib.sha256((str(checkout) + "\0" + deployment).encode()).hexdigest()[:20]


def _bind(source, target, *, readonly=True):
    return {"type": "bind", "source": str(source), "target": str(target), "read_only": readonly,
            "bind": {"create_host_path": False}}


def _provider_stop_seconds(config):
    """Allow existing shutdown attempts; an unresolved owner still requires kill.

    service._shutdown joins work, Hello, maintenance and controller settlement,
    then retires both API owners and settles storage. process._settle_borrowers
    can observe that sequence twice around factory settlement. Factory owns at
    most one incomplete serial setup. Startup admission can finish its bounded
    inventory traversal before noticing stop. This allowance is physical process
    retirement, never a receipt for durable Attempt completion (note 021).
    """
    lifecycle, policy, native = config.lifecycle, config.service.policy, config.service.native
    retirement = config.api.http.retirement_seconds
    settlement = ((2 + lifecycle.live_limits.maximum_attempts) * policy.settle_seconds
                  + 3 * retirement)
    draining_shutdown = 2 * policy.drain_seconds + settlement
    setup_settlement = 2 * native.allocation_seconds + max(
        native.prediction_seconds, native.source_seconds, config.preparation.generation_seconds)
    startup = config.api.http.exchange_seconds + retirement
    final_dependencies = retirement
    return sum((startup, draining_shutdown, 2 * settlement, setup_settlement, final_dependencies))


def _service(image, interpreter, module, *, uid, gid, labels, stop_seconds, memory="512m"):
    return {
        "image": image, "pull_policy": "never", "entrypoint": [interpreter, "-I", "-B", "-m", module],
        "user": f"{uid}:{gid}", "init": True, "read_only": True, "cap_drop": ["ALL"],
        "ipc": "private", "cgroup": "private",
        "security_opt": ["no-new-privileges:true"], "cpus": 2, "mem_limit": memory,
        "memswap_limit": memory, "pids_limit": 64, "restart": "unless-stopped",
        "stop_grace_period": f"{math.ceil(stop_seconds)}s",
        "network_mode": "none", "labels": dict(labels), "environment": {"NMRNET_LOG_LEVEL": "INFO"},
        "tmpfs": [f"/{name}:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid={uid},gid={gid}" for name in ("tmp", "run")],
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3",
                    "mode": "non-blocking", "max-buffer-size": "1m"}},
    }


def _disjoint(first, second):
    if first.is_relative_to(second) or second.is_relative_to(first):
        raise ConfigurationRejected("independent deployment grants overlap; keep the lifetime lock, frozen inputs and runtime leaves separate.")


def render_compose(config, *, deployment, checkout, bundle, uid, gid, prepared, execution, routes, grants, shared_image_id):
    """Return the fixed services without touching Engine or acquiring work.

    Role locks and directory privacy are admitted/created by the finite host owner.
    Only the matching runtime leaf enters each native runner; only the provider
    receives frozen credentials and the writable lifetime-lock volume. Native scratch and
    per-start process diagnostics are private tmpfs, never host log dump paths.
    """
    if type(deployment) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", deployment) is None:
        raise ConfigurationRejected("deployment requires an explicit name of up to 48 letters, digits, underscores or hyphens.")
    if any(type(value) is not int or not 1 <= value <= 4294967294 for value in (uid, gid)):
        raise ConfigurationRejected("deployment requires an explicit nonroot caller UID and GID.")
    if type(shared_image_id) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", shared_image_id) is None:
        raise ConfigurationRejected("deployment requires the shared runtime's full immutable image ID.")
    checkout, bundle, prepared = map(_bind_path, (checkout, bundle, prepared))
    if bundle.parent.name != "bundles" or bundle.parent.parent.name != deployment:
        raise ConfigurationRejected("frozen bundle must belong to this deployment's bundles directory.")
    runtime = bundle.parent.parent / "runtime"
    lifetime_root = _bind_path(config.lifecycle.lifetime_root)
    for other in (bundle, runtime, prepared, checkout):
        _disjoint(lifetime_root, other)
    for root in (bundle, lifetime_root):
        if root == root.parent or str(root) in ("/tmp", "/run") or str(root).split("/")[1] in (
            "bin", "sbin", "lib", "lib64", "usr", "opt", "etc", "proc", "sys", "dev", "native", "routes", "input", "diagnostics", "run"
        ):
            raise ConfigurationRejected("deployment input/lifetime mounts must not mask image software, trust or fixed runtime grants.")
    socket_root = _bind_path(config.delivery.endpoint.socket_path.parent)
    protected = (bundle, lifetime_root, *(Path("/") / name for name in (
        "bin", "sbin", "lib", "lib64", "usr", "opt", "etc", "proc", "sys", "dev", "native", "routes", "input"
    )), Path("/run/status.json"))
    if socket_root == Path("/tmp") or any(
        socket_root.is_relative_to(path) or path.is_relative_to(socket_root) for path in protected
    ):
        raise ConfigurationRejected("delivery socket directory must not overlap provider software, frozen inputs, lifetime lock or runtime grants.")
    identity = _declared_execution(execution)
    if type(routes) is not dict or set(routes) != {"interpreter", "store"}:
        raise ConfigurationRejected("deployment requires exactly the interpreter and store routes; the host owns the API route.")
    labels = {"io.nmrnet-repro.deployment": deployment, "io.nmrnet-repro.bundle": bundle.name,
              "io.nmrnet-repro.checkout": str(checkout)}
    project = project_name(checkout, deployment)
    controller_python = "/opt/provider/environment/bin/python"
    services = {}
    provider = _service(identity["controller_image_id"], controller_python, "nmrnet_provider.process", uid=uid, gid=gid, labels=labels,
                        stop_seconds=_provider_stop_seconds(config))
    provider["depends_on"] = {role: {"condition": "service_started"} for role in ("prediction", "reader", "smiles", "interpreter", "store")}
    provider["command"] = ["--config", str(bundle / "config/provider.toml"), "--execution", "/input/execution.json",
                           "--models", "/input/prediction-models.json", "--diagnostics", "/run"]
    # Keep this scope's physical volume identity so supervisors contend for
    # the same lifetime lock across container replacement.
    control_volume = checkpoint_volume_name(config.lifecycle.scope, namespace=grants.project)
    provider["volumes"] = [_bind(bundle, bundle), _bind(bundle / "execution.json", "/input/execution.json"),
                           _bind(bundle / "prediction-models.json", "/input/prediction-models.json"),
                           {"type": "volume", "source": control_volume, "target": str(lifetime_root), "read_only": False,
                            "volume": {"nocopy": True}}]
    if (config.delivery.endpoint.socket_path, config.delivery.endpoint.socket_group_id) != (grants.outbox_socket, grants.gid):
        raise ConfigurationRejected("delivery endpoint must match the shared outbox socket grant.")
    shared = grants.compose()
    provider["volumes"].extend(shared["mounts"])
    provider["group_add"] = shared["group_add"]
    provider["command"] += ["--api-proxy-socket", str(grants.proxy_socket)]
    services["provider"] = provider
    native = config.service.native
    for role in ("prediction", "reader", "smiles"):
        prediction = role == "prediction"
        interpreter = "/usr/local/bin/python" if prediction else "/opt/rdkit/environment/bin/python"
        image = identity["image_id"] if prediction else identity["geometry_image_id"]
        service = _service(image, interpreter, "nmrnet_provider.worker_main", uid=uid, gid=gid, labels=labels,
                           stop_seconds=native.allocation_seconds, memory="4g" if prediction else "512m")
        seconds = prediction_lifetime_seconds(config) if prediction else (
            native.source_seconds if role == "reader" else config.preparation.generation_seconds)
        lifetime = seconds if prediction else native.allocation_seconds + seconds
        # SIGTERM during greeting can spend startup twice: greeting delivery,
        # then retirement delivery around TERM/KILL/scratch cleanup. All five
        # stages together fit the existing allocation cleanup allowance.
        grace = native.phase_seconds
        service["command"] = [role, "--socket", f"/native/{role}/runner.sock", "--scratch", "/scratch",
                              "--io-seconds", str(seconds), "--operation-seconds", str(lifetime),
                              "--startup-seconds", str(grace), "--terminate-seconds", str(grace),
                              "--kill-seconds", str(grace), "--cleanup-seconds", str(grace)]
        if role == "smiles":
            generation = config.preparation.generation_settings
            service["command"] += ["--seed", str(generation.seed), "--max-iterations", str(generation.max_iterations),
                                   "--embedding-seconds", str(generation.timeout_seconds)]
        service["volumes"] = [_bind(runtime / role, f"/native/{role}", readonly=False)]
        if prediction:
            service["volumes"] += [_bind(bundle / name, f"/input/{name}") for name in (
                "prediction-models.json", "inference.lock", "unicore-source.json")]
            service["volumes"] += [_bind(prepared / name, "/" + name) for name in ("weight", "checkpoints")]
        service["tmpfs"].append(f"/scratch:rw,nosuid,nodev,noexec,size=64m,mode=0700,uid={uid},gid={gid}")
        services[role] = service
        provider["volumes"].append(_bind(runtime / role, f"/native/{role}"))
    authorities = startup_authorities(config)
    networks = {}
    for role in ("interpreter", "store"):
        route = routes[role]
        if type(route) is not dict or not {"mode", "interface", "log_level"} <= set(route) or set(route) - {"mode", "interface", "log_level", "addresses", "network"}:
            raise ConfigurationRejected("each deployment route requires mode/interface/log_level and only its applicable address/network grants.")
        if route["log_level"] not in ("INFO", "DEBUG", "WARNING"):
            raise ConfigurationRejected("route log_level must be INFO, DEBUG or WARNING.")
        interface = route["interface"]
        if type(interface) is not str:
            raise ConfigurationRejected("each route requires an explicit network interface name of at most 15 characters.")
        try:
            require_interface_name(interface)
        except ValueError as cause:
            raise ConfigurationRejected(str(cause), cause=cause) from None
        addresses = route.get("addresses", [])
        if type(addresses) is not list or any(type(value) is not str for value in addresses):
            raise ConfigurationRejected("route addresses require explicit host=IPv4 strings.")
        try:
            ServicePolicy.admit(route["mode"], authorities[role], addresses)
        except ServiceRefused as cause:
            raise ConfigurationRejected(str(cause), cause=cause) from None
        # Proxies use SIGTERM's immediate process exit, after provider drain.
        # Five seconds allows Engine/init scheduling, not an HTTP retry window.
        service = _service(shared_image_id, "/opt/provider-core/environment/bin/python", "provider_core.provider_proxy", uid=uid, gid=gid, labels=labels,
                           stop_seconds=5)
        service["command"] = ["--mode", route["mode"], "--interface", interface, "--socket", "/route/proxy.sock",
                              "--ready-file", "/route/ready", "--status-directory", "/run", "--log-level", route["log_level"]]
        for value in authorities[role]:
            service["command"] += ["--authority", value]
        for value in addresses:
            service["command"] += ["--address", value]
        service["volumes"] = [_bind(runtime / ("route-" + role), "/route", readonly=False)]
        if route["mode"] == "local":
            network = route.get("network")
            if type(network) is not str or re.fullmatch(r"[0-9a-f]{64}", network) is None:
                raise ConfigurationRejected("local routing requires the observed full immutable Docker network ID.")
            service.pop("network_mode")
            service["networks"] = [role]
            networks[role] = {"external": True, "name": network}
        else:
            if "network" in route:
                raise ConfigurationRejected("only local routing accepts a Docker network grant.")
            service["network_mode"] = "host"
            if route["mode"] == "external":
                service["volumes"].append(_bind(bundle / "resolv.conf", "/etc/resolv.conf"))
        services[role] = service
        provider["volumes"].append(_bind(runtime / ("route-" + role), f"/routes/{role}"))
        provider["command"] += [f"--{role}-proxy-socket", f"/routes/{role}/proxy.sock"]
    for role, service in services.items():
        # Fresh install names make a lost create reply discoverable. All later
        # control still uses only admitted full IDs, never a name as authority.
        service["container_name"] = project + "-" + role + "-" + bundle.name
        service["labels"].update(labels)
        status_role = "proxy" if role in ("interpreter", "store") else role
        status_path = "/run/status.json"
        startup = native.allocation_seconds
        if status_role == "proxy":
            startup = 5
        if role == "provider":
            startup = 2 * (config.api.http.exchange_seconds + config.api.http.retirement_seconds)
        service["healthcheck"] = {"test": ["CMD", service["entrypoint"][0], "-I", "-B", "-m",
                                                  "provider_core.process_status", status_path, status_role],
                                  "interval": "5s", "timeout": "2s", "retries": 3,
                                  "start_period": f"{math.ceil(startup)}s"}
    return {"name": project, "services": services, "networks": networks,
            "volumes": shared["volumes"] | {control_volume: {"name": control_volume, "external": True}}}


def compose_bytes(plan):
    """Escape literal dollars at Compose's interpolation boundary, including paths."""
    return (json.dumps(plan, sort_keys=True).replace("$", "$$") + "\n").encode()
