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

from nmrnet_provider.config import ConfigurationRejected
from nmrnet_provider.process import _declared_execution
from nmrnet_provider.startup_capture import _bind_path, _host_plan, prediction_lifetime_seconds
from tools.provider_proxy import ServicePolicy, ServiceRefused
from tools.bound_http_proxy import require_interface_name


def project_name(checkout, deployment):
    """Keep project discovery stable across immutable bundle generations."""
    return "nmrnet-" + hashlib.sha256((str(checkout) + "\0" + deployment).encode()).hexdigest()[:20]


def inventory_containers(records, *, requested_ids, checkout, deployment):
    """Admit actual project IDs and original bundle labels, including stopped subsets.

    Adapted from SECS 7356c354368f2355d3ab5cef00cf48c67cea9c2c,
    deployment/compose.py::ComposeProject.inventory:
    https://github.com/numpde/secs-repro/blob/7356c354368f2355d3ab5cef00cf48c67cea9c2c/deployment/compose.py
    The host
    must next compare each container's full grants with its original bundle plan;
    selecting a newer bundle does not authorize controlling a foreign old one.
    This returns bounded internal records, never a public inspect-JSON response.
    """
    roles = {"provider", "prediction", "reader", "smiles", "api", "interpreter", "store"}
    if type(requested_ids) is not list or len(requested_ids) > len(roles) or any(
        type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None for value in requested_ids
    ) or len(set(requested_ids)) != len(requested_ids):
        raise ConfigurationRejected("deployment inventory requires at most seven distinct full container IDs from the project query.")
    if type(records) is not list or len(records) != len(requested_ids):
        raise ConfigurationRejected("deployment inspection did not return exactly the requested project containers.")
    project = project_name(checkout, deployment)
    admitted, seen = {}, set()
    for record in records:
        if type(record) is not dict or type(record.get("Config")) is not dict or type(record.get("State")) is not dict:
            raise ConfigurationRejected("deployment inspection requires container configuration and state objects.")
        identity, labels, state = record.get("Id"), record["Config"].get("Labels"), record["State"]
        if type(identity) is not str or identity not in requested_ids or identity in seen:
            raise ConfigurationRejected("deployment inspection returned a duplicate, unrequested or malformed container ID.")
        if type(labels) is not dict:
            raise ConfigurationRejected("deployment container ownership labels are missing or malformed.")
        role = labels.get("com.docker.compose.service")
        if type(role) is not str or role not in roles or role in admitted:
            raise ConfigurationRejected("deployment inventory contains an unknown or duplicate service role; preserve the exact containers.")
        required = {"com.docker.compose.project": project, "com.docker.compose.oneoff": "False",
                    "com.docker.compose.project.working_dir": str(checkout),
                    "io.nmrnet-repro.checkout": str(checkout), "io.nmrnet-repro.deployment": deployment}
        if any(labels.get(name) != value for name, value in required.items()):
            raise ConfigurationRejected("deployment container labels do not match this checkout, project and deployment; no control is authorized.")
        bundle = labels.get("io.nmrnet-repro.bundle")
        if type(bundle) is not str or re.fullmatch(r"generation-[A-Za-z0-9_-]{1,64}", bundle) is None:
            raise ConfigurationRejected("deployment container requires an original generation basename within its retained bundle root.")
        if type(state.get("Running")) is not bool or type(state.get("Restarting")) is not bool or type(state.get("Pid")) is not int or state["Pid"] < 0:
            raise ConfigurationRejected("deployment inspection requires explicit running, restarting and process observations.")
        seen.add(identity)
        admitted[role] = record
    return admitted


def _environment(values):
    if type(values) is not list:
        raise ConfigurationRejected("container/image environment must be an explicit list of assignments.")
    result = {}
    for value in values:
        if type(value) is not str or "=" not in value:
            raise ConfigurationRejected("container/image environment contains a malformed assignment.")
        name, content = value.split("=", 1)
        if not name or name in result:
            raise ConfigurationRejected("container/image environment contains an empty or duplicate name.")
        result[name] = content
    return result


def _tmpfs_options(value):
    """Normalize only the private 64 MiB mounts emitted by this recipe."""
    if type(value) is not str:
        raise ConfigurationRejected("deployment tmpfs options must be explicit strings.")
    options = {}
    for item in value.split(","):
        name, separator, content = item.partition("=")
        if name in options:
            raise ConfigurationRejected("deployment tmpfs options contain a duplicate setting.")
        options[name] = content if separator else None
    if set(options) != {"rw", "nosuid", "nodev", "noexec", "size", "mode", "uid", "gid"} or any(
        options[name] is not None for name in ("rw", "nosuid", "nodev", "noexec")
    ):
        raise ConfigurationRejected("deployment tmpfs must retain exactly its private ownership, capacity and non-executable mount flags.")
    if options["size"] not in ("64m", "67108864") or options["mode"] not in ("700", "0700"):
        raise ConfigurationRejected("deployment tmpfs capacity or private mode differs from the fixed recipe.")
    return options["uid"], options["gid"]


def _stop_signal(config):
    # silent-defaults: allow-contract -- Docker's absent/empty stop signal means SIGTERM; numeric 15 is the same Linux signal, not a different shutdown policy.
    signal = config.get("StopSignal", "")
    if type(signal) is not str:
        raise ConfigurationRejected("container/image stop signal must be a string when declared.")
    return "SIGTERM" if signal in ("", "SIGTERM", "15") else signal


def _grant_summary(value):
    """Describe bounded scalar evidence without echoing environment/command text."""
    if value is None:
        return "absent"
    if type(value) is bool:
        return str(value).lower()
    if type(value) is int:
        return str(value) if -(2**63) <= value < 2**63 else "integer outside signed 64-bit range"
    if type(value) is str and value in ("", "host", "private", "none", "shareable", "unless-stopped", "always", "json-file", "journald"):
        return repr(value)
    if type(value) in (list, dict):
        return f"{type(value).__name__} with {len(value)} entries"
    return type(value).__name__


def _refuse_grant(role, field, expected, observed):
    raise ConfigurationRejected(
        f"{role} {field} differs from its admitted plan: expected {_grant_summary(expected)}; observed {_grant_summary(observed)}."
    )


def admit_container(record, *, role, original_plan, image):
    """Compare actual creation grants with the retained plan and immutable image.

    Engine owns inspect observations. Image defaults own inherited environment,
    workdir, signal and healthcheck; this recipe owns explicit overrides. Reject
    extra grants as well as missing ones. Dynamic PIDs/timestamps/IPs are not
    creation identities. Call only after inventory_containers admitted ownership.
    """
    service = original_plan["services"][role]
    if type(image) is not dict or image.get("Id") != service["image"] or type(image.get("Config")) is not dict:
        raise ConfigurationRejected("deployment image inspection does not identify the original plan's immutable image.")
    if record.get("Image") != service["image"] or type(record.get("Config")) is not dict or type(record.get("HostConfig")) is not dict:
        raise ConfigurationRejected("deployment container image or creation configuration differs from its original plan.")
    actual, host, defaults = record["Config"], record["HostConfig"], image["Config"]
    if record.get("Name") != "/" + service["container_name"]:
        raise ConfigurationRejected(f"{role} container name does not identify its original creation generation.")
    if type(actual.get("Labels")) is not dict or any(actual["Labels"].get(name) != value for name, value in service["labels"].items()):
        raise ConfigurationRejected("deployment container does not belong to the original bundle plan supplied for grant admission.")
    expected_config = {"Image": service["image"], "Entrypoint": service["entrypoint"], "Cmd": service["command"],
                       "User": service["user"], "WorkingDir": defaults["WorkingDir"],
                       "StopTimeout": int(service["stop_grace_period"].removesuffix("s"))}
    for name, value in expected_config.items():
        if actual.get(name) != value:
            _refuse_grant(role, "Config." + name, value, actual.get(name))
    for name in ("OpenStdin", "Tty"):
        if actual.get(name) is not False:
            _refuse_grant(role, "Config." + name, False, actual.get(name))
    observed_environment = _environment(actual.get("Env"))
    expected_environment = _environment(defaults.get("Env")) | service["environment"]
    if observed_environment != expected_environment:
        extra = len(observed_environment.keys() - expected_environment.keys())
        missing = len(expected_environment.keys() - observed_environment.keys())
        changed = sum(observed_environment[name] != expected_environment[name] for name in observed_environment.keys() & expected_environment.keys())
        raise ConfigurationRejected(f"{role} Config.Env differs from image defaults and plan overrides: {extra} extra, {missing} missing, {changed} changed assignments.")
    health = service["healthcheck"]
    expected_health = {"Test": health["test"], "Interval": 5_000_000_000, "Timeout": 2_000_000_000,
                       "Retries": 3, "StartPeriod": int(health["start_period"][:-1]) * 1_000_000_000}
    if _stop_signal(actual) != _stop_signal(defaults) or actual.get("Healthcheck") != expected_health:
        raise ConfigurationRejected("deployment container signal or healthcheck adds an unplanned execution path.")
    for name, value in (("ReadonlyRootfs", True), ("Privileged", False), ("Init", True)):
        if host.get(name) is not value:
            _refuse_grant(role, "HostConfig." + name, value, host.get(name))
    if host.get("CapDrop") != ["ALL"]:
        _refuse_grant(role, "HostConfig.CapDrop (ALL required)", ["ALL"], host.get("CapDrop"))
    if host.get("CapAdd") not in (None, []):
        _refuse_grant(role, "HostConfig.CapAdd", [], host.get("CapAdd"))
    if host.get("SecurityOpt") not in (["no-new-privileges:true"], ["no-new-privileges"]):
        _refuse_grant(role, "HostConfig.SecurityOpt (no-new-privileges required)", ["no-new-privileges:true"], host.get("SecurityOpt"))
    for name in ("Devices", "DeviceRequests", "DeviceCgroupRules", "GroupAdd", "ExtraHosts", "Dns", "DnsOptions", "DnsSearch", "Links", "VolumesFrom"):
        # Docker serializes empty optional list grants as either null or [].
        # Any populated value is an extra grant; no image default supplies one.
        if host.get(name) not in (None, []):
            _refuse_grant(role, "HostConfig." + name, [], host.get(name))
    for name, value in (("PidMode", ""), ("UTSMode", ""), ("UsernsMode", ""), ("IpcMode", "private"), ("CgroupnsMode", "private")):
        if host.get(name) != value:
            _refuse_grant(role, "HostConfig." + name, value, host.get(name))
    if host.get("PortBindings") not in (None, {}) or host.get("PublishAllPorts") is not False:
        raise ConfigurationRejected("deployment container publishes unplanned host ports.")
    if host.get("Sysctls") not in (None, {}):
        raise ConfigurationRejected("deployment container adds unplanned kernel settings.")
    # These minimum kernel restrictions were observed in the local Engine's
    # default unprivileged profile on 24 October 2026 (note 021). Additional
    # masks/read-only paths restrict authority further; they cannot replace a
    # required restriction. Do not depend on their serialization order.
    restrictions = {
        "MaskedPaths": {"/proc/acpi", "/proc/asound", "/proc/interrupts", "/proc/kcore", "/proc/keys", "/proc/latency_stats",
                        "/proc/sched_debug", "/proc/scsi", "/proc/timer_list", "/proc/timer_stats", "/sys/devices/virtual/powercap", "/sys/firmware"},
        "ReadonlyPaths": {"/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys", "/proc/sysrq-trigger"},
    }
    for name, minimum in restrictions.items():
        observed = host.get(name)
        if type(observed) is not list or any(type(path) is not str for path in observed) or len(set(observed)) != len(observed) or not minimum <= set(observed):
            raise ConfigurationRejected("deployment container removes required kernel-path masks or read-only restrictions.")
    memory = {"512m": 536870912, "4g": 4294967296}[service["mem_limit"]]
    for name, value in {"Memory": memory, "MemorySwap": memory, "NanoCpus": 2000000000, "PidsLimit": 64}.items():
        if type(host.get(name)) is not int or host[name] != value:
            _refuse_grant(role, "HostConfig." + name, value, host.get(name))
    if host.get("RestartPolicy") != {"Name": service["restart"], "MaximumRetryCount": 0}:
        raise ConfigurationRejected("deployment container restart policy differs from its original plan.")
    if host.get("LogConfig") != {"Type": service["logging"]["driver"], "Config": service["logging"]["options"]}:
        raise ConfigurationRejected("deployment container log delivery or rotation differs from its bounded Docker log policy.")
    expected_binds = {grant["target"]: grant for grant in service["volumes"]}
    expected_tmpfs = dict(value.split(":", 1) for value in service["tmpfs"])
    mounts = record.get("Mounts")
    if type(mounts) is not list:
        raise ConfigurationRejected("deployment inspection is missing its complete mount inventory.")
    seen, binds = set(), set()
    for mount in mounts:
        if type(mount) is not dict or type(mount.get("Destination")) is not str or mount["Destination"] in seen:
            raise ConfigurationRejected("deployment mount inventory contains a malformed or duplicate destination.")
        target = mount["Destination"]
        seen.add(target)
        if mount.get("Type") == "tmpfs" and target in expected_tmpfs and mount.get("RW") is True:
            continue
        if mount.get("Type") != "bind" or target not in expected_binds:
            raise ConfigurationRejected("deployment container has an extra mount or replaces a required tmpfs with host storage.")
        grant = expected_binds[target]
        expected_writable = not grant["read_only"]
        if mount.get("Source") != grant["source"] or type(mount.get("RW")) is not bool or mount["RW"] != expected_writable:
            raise ConfigurationRejected("deployment bind source or write permission differs from its original role grant.")
        if mount.get("Propagation") not in ("", "rprivate") or type(mount.get("Mode")) is not str or set(filter(None, mount["Mode"].split(","))) - {"ro", "rw", "rprivate"}:
            raise ConfigurationRejected("deployment bind mount adds unplanned propagation or relabeling behavior.")
        binds.add(target)
    if binds != set(expected_binds):
        raise ConfigurationRejected("deployment container is missing a required bind grant.")
    tmpfs = host.get("Tmpfs")
    if type(tmpfs) is not dict or set(tmpfs) != set(expected_tmpfs):
        raise ConfigurationRejected("deployment container tmpfs mount kinds or destinations differ from its fixed role.")
    for target, options in tmpfs.items():
        if _tmpfs_options(options) != _tmpfs_options(expected_tmpfs[target]):
            raise ConfigurationRejected("deployment tmpfs owner differs from the admitted nonroot caller.")
    settings = record.get("NetworkSettings")
    if type(settings) is not dict or type(settings.get("Networks")) is not dict:
        raise ConfigurationRejected("deployment inspection is missing its complete network attachment inventory.")
    attached = settings["Networks"]
    if "network_mode" in service:
        mode = service["network_mode"]
        if host.get("NetworkMode") != mode or set(attached) not in (set(), {mode}):
            raise ConfigurationRejected("deployment container has an extra network or differs from its admitted isolated/host mode.")
    else:
        network = original_plan["networks"][service["networks"][0]]["name"]
        if len(attached) != 1 or any(type(value) is not dict or value.get("NetworkID") != network for value in attached.values()) or host.get("NetworkMode") not in {*attached, network}:
            raise ConfigurationRejected("deployment local proxy is not attached exclusively to its admitted external network ID.")


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
    store, retirement = lifecycle.store_seconds, config.api.http.retirement_seconds
    settlement = 4 * policy.settle_seconds + 2 * retirement + store
    draining_shutdown = 2 * policy.drain_seconds + settlement
    setup_settlement = store + 2 * native.allocation_seconds + max(
        native.prediction_seconds, native.source_seconds, config.preparation.generation_seconds)
    startup = store + policy.admission.maximum_pages * (config.api.http.exchange_seconds + retirement) + retirement
    final_dependencies = store + retirement + store
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
        raise ConfigurationRejected("independent deployment grants overlap; keep store, frozen inputs and runtime leaves separate.")


def render_compose(config, *, deployment, checkout, bundle, uid, gid, prepared, execution, routes):
    """Return the seven fixed services without touching Engine or acquiring work.

    Role locks and directory privacy are admitted/created by the finite host owner.
    Only the matching runtime leaf enters each native runner; only the provider
    receives frozen credentials and the writable Attempt store. Native scratch and
    per-start process diagnostics are private tmpfs, never host log dump paths.
    """
    if type(deployment) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", deployment) is None:
        raise ConfigurationRejected("deployment requires an explicit name of up to 48 letters, digits, underscores or hyphens.")
    if any(type(value) is not int or value < 1 for value in (uid, gid)):
        raise ConfigurationRejected("deployment requires an explicit nonroot caller UID and GID.")
    checkout, bundle, prepared = map(_bind_path, (checkout, bundle, prepared))
    if bundle.parent.name != "bundles" or bundle.parent.parent.name != deployment:
        raise ConfigurationRejected("frozen bundle must belong to this deployment's bundles directory.")
    runtime = bundle.parent.parent / "runtime"
    store = _bind_path(config.lifecycle.store_root)
    for other in (bundle, runtime, prepared, checkout):
        _disjoint(store, other)
    for root in (bundle, store):
        if root == root.parent or str(root) in ("/tmp", "/run") or str(root).split("/")[1] in (
            "bin", "sbin", "lib", "lib64", "usr", "opt", "etc", "proc", "sys", "dev", "native", "routes", "input", "diagnostics"
        ):
            raise ConfigurationRejected("deployment input/store mounts must not mask image software, trust or fixed runtime grants.")
    identity = _declared_execution(execution)
    if type(routes) is not dict or set(routes) != {"api", "interpreter", "store"}:
        raise ConfigurationRejected("deployment requires exactly the api, interpreter and store routes.")
    labels = {"io.nmrnet-repro.deployment": deployment, "io.nmrnet-repro.bundle": bundle.name,
              "io.nmrnet-repro.checkout": str(checkout)}
    project = project_name(checkout, deployment)
    controller_python = "/opt/provider/environment/bin/python"
    services = {}
    provider = _service(identity["controller_image_id"], controller_python, "nmrnet_provider.process", uid=uid, gid=gid, labels=labels,
                        stop_seconds=_provider_stop_seconds(config))
    provider["depends_on"] = {role: {"condition": "service_started"} for role in ("prediction", "reader", "smiles", "api", "interpreter", "store")}
    provider["command"] = ["--config", str(bundle / "config/provider.toml"), "--execution", "/input/execution.json",
                           "--models", "/input/prediction-models.json", "--diagnostics", "/run"]
    provider["volumes"] = [_bind(bundle, bundle), _bind(bundle / "execution.json", "/input/execution.json"),
                           _bind(bundle / "prediction-models.json", "/input/prediction-models.json"), _bind(store, store, readonly=False)]
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
        grace = native.allocation_seconds / 5
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
    authorities = _host_plan(config, bundle, bundle / "config/provider.toml")["authorities"]
    networks = {}
    for role in ("api", "interpreter", "store"):
        route = routes[role]
        if type(route) is not dict or not {"mode", "interface"} <= set(route) or set(route) - {"mode", "interface", "addresses", "network"}:
            raise ConfigurationRejected("each deployment route requires mode/interface and only its applicable address/network grants.")
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
        service = _service(identity["controller_image_id"], controller_python, "tools.provider_proxy", uid=uid, gid=gid, labels=labels,
                           stop_seconds=5)
        service["command"] = ["--mode", route["mode"], "--interface", interface, "--socket", "/route/proxy.sock",
                              "--ready-file", "/route/ready", "--status-directory", "/run"]
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
        status_role = "proxy" if role in ("api", "interpreter", "store") else role
        status_path = "/run/status.json"
        startup = native.allocation_seconds
        if status_role == "proxy":
            startup = 5
        if role == "provider":
            startup = config.lifecycle.store_seconds + (config.service.policy.admission.maximum_pages + 1) * (
                config.api.http.exchange_seconds + config.api.http.retirement_seconds)
        service["healthcheck"] = {"test": ["CMD", service["entrypoint"][0], "-I", "-B", "-m",
                                                  "provider_core.process_status", status_path, status_role],
                                  "interval": "5s", "timeout": "2s", "retries": 3,
                                  "start_period": f"{math.ceil(startup)}s"}
    return {"name": project, "services": services, "networks": networks}


def compose_bytes(plan):
    """Escape literal dollars at Compose's interpolation boundary, including paths."""
    return (json.dumps(plan, sort_keys=True).replace("$", "$$") + "\n").encode()
