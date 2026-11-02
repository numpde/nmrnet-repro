"""Freeze a public deployment definition with its separately protected inputs.

Layout and bundle ownership follow magnet-deploy 0619d9ffd92a4326758b442c532aa09496161098,
scripts/provider-deployment.sh::stage_deployment_input_bundle. NMRNet's existing
startup owner accepts one private source root. Rebase only declared input paths
into that runtime configuration; preserve the reviewed public definition and
admit the result through existing owners.
"""

from tools.container_guard import require_container

require_container()

import argparse
import ipaddress
import json
import os
import re
import stat
import sys
from pathlib import Path

import tomllib
import tomli_w

from nmrnet_provider.config import ConfigurationRejected, _read_provider_config, read_startup_file
from nmrnet_provider.deployment import _disjoint, compose_bytes, render_compose
from nmrnet_provider.launch_capture import capture_execution
from nmrnet_provider.process import _declared_models
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.startup_capture import _bind_path, _source_path
from provider_core.response_json import ResponseRejected, read_document
from provider_core.chat import ChatEndpoint
from provider_core.shared_grants import SharedGrants

INPUT_ROOT = Path("/srv/nmrnet/input")


def _write(path, raw):
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def freeze_deployment(definition, settings, secrets, bundle, source_root, models,
                      *, source_names, deployment, checkout, uid, gid, prepared,
                      shared_image_id, **identities):
    """Freeze admitted inputs into a fresh generation for Compose normalization.

    No API/model request, store opening or worker allocation occurs. Failed
    generations retain their actual configuration. After selection, the launcher
    retires older generations only when no container borrows them.
    The caller receives the resulting Compose configuration directly.
    """
    definition, settings, secrets, bundle, source_root, models = map(
        _bind_path, (definition, settings, secrets, bundle, source_root, models)
    )
    expected_directories = (bundle / "config", bundle / "secrets", bundle / "secrets" / "provider")
    for directory in expected_directories:
        observed = directory.lstat()
        if not stat.S_ISDIR(observed.st_mode) or observed.st_uid != os.geteuid() or stat.S_IMODE(observed.st_mode) != 0o700:
            raise ConfigurationRejected("the fresh deployment bundle requires caller-owned mode-0700 config and secret directories.")
    with os.scandir(bundle / "secrets") as entries:
        secret_children = list(entries)
    if len(secret_children) != 1 or secret_children[0].name != "provider" or not secret_children[0].is_dir(follow_symlinks=False):
        raise ConfigurationRejected("choose a fresh deployment bundle with only its provider secret directory; existing contents were preserved.")
    for directory in (bundle / "config", bundle / "secrets" / "provider"):
        with os.scandir(directory) as entries:
            if next(entries, None) is not None:
                raise ConfigurationRejected("choose fresh empty deployment input directories; existing generation contents were preserved.")
    raw = read_startup_file(definition, maximum_bytes=65536, label="public deployment definition")
    local_raw = read_startup_file(settings, maximum_bytes=65536, label="private deployment settings", secret=True)
    try:
        document = tomllib.loads(raw.decode("utf-8"))
        local = tomllib.loads(local_raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ConfigurationRejected("provider and deployment definitions must contain valid UTF-8 TOML.") from None
    if not {"maximum_startup_bytes", "routes", "deployment"} <= set(local) or set(local) - {
        "maximum_startup_bytes", "routes", "resolvers", "deployment"
    }:
        raise ConfigurationRejected("deployment.toml requires maximum_startup_bytes, routes and shared deployment grants; only external routing accepts resolvers.")
    grant_input = local["deployment"]
    project = grant_input.get("project") if type(grant_input) is dict else None
    group_id = grant_input.get("gid") if type(grant_input) is dict else None
    if (type(project) is not str or re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,48}", project) is None
            or type(group_id) is not int or not 0 < group_id <= 4294967294):
        raise ConfigurationRejected("deployment.toml [deployment] requires the shared project name and nonzero socket group ID.")
    grants = SharedGrants(project, group_id)
    if "delivery" not in document or type(document["delivery"]) is not dict:
        raise ConfigurationRejected("provider definition requires a delivery table with its IPC intake policy.")
    if "api" in document:
        raise ConfigurationRejected("API identity and trust settings must come from the provider's private api.toml.")
    try:
        api_raw = read_startup_file(_source_path(secrets / "api.toml", secrets), maximum_bytes=65536,
                                    label="provider API settings", secret=True)
        document["api"] = tomllib.loads(api_raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ConfigurationRejected("provider API settings require valid UTF-8 TOML.") from None
    if {"socket_path", "socket_group_id"} & document["delivery"].keys():
        raise ConfigurationRejected("delivery socket grants come from deployment.toml [deployment].")
    document["delivery"].update(socket_path=str(grants.outbox_socket), socket_group_id=grants.gid)
    raw = tomli_w.dumps(document).encode("utf-8")
    maximum = local["maximum_startup_bytes"]
    if type(maximum) is not int or maximum < 1:
        raise ConfigurationRejected("deployment maximum_startup_bytes must be a positive integer.")
    if type(local["routes"]) is not dict or set(local["routes"]) != {"interpreter", "store"}:
        raise ConfigurationRejected("deployment.toml owns only interpreter/store routes; the host owns the API route.")
    routes = dict(local["routes"])
    consumed, retained_provider = {}, {}

    def retain_input(actual, leaf, **limits):
        content = read_startup_file(actual, **limits)
        if leaf in consumed:
            if consumed[leaf] != content:
                raise ConfigurationRejected("a repeated deployment input changed while freezing; preserve this failed bundle.")
        else:
            _write(bundle / "secrets" / leaf, content)
            consumed[leaf] = content
        return content

    def retain_provider(path, **limits):
        path = _bind_path(path)
        if path == definition:
            return raw
        if not path.is_relative_to(INPUT_ROOT) or len(path.relative_to(INPUT_ROOT).parts) != 1:
            raise ConfigurationRejected("provider key and CA paths must name files directly under /srv/nmrnet/input/.")
        leaf = Path("provider") / path.name
        retained_provider[path] = bundle / "secrets" / leaf
        return retain_input(_source_path(secrets / path.name, secrets), leaf, **limits)

    admitted = _read_provider_config(definition, retain_provider, read_api_file=retain_provider)
    if Path(admitted.lifecycle.lifetime_root) != Path("/srv/nmrnet/attempts"):
        raise ConfigurationRejected(
            "the public deployment lifetime_root must be /srv/nmrnet/attempts, the provider lifetime volume's container path."
        )
    # Only admitted input paths change; the writer preserves the parsed scientific
    # values without imposing scalar-line or table-layout restrictions on TOML.
    document["api"]["credential_file"] = str(retained_provider[Path(document["api"]["credential_file"])])
    for endpoint in document["interpreter"]["endpoints"]:
        endpoint["key_file"] = str(retained_provider[Path(endpoint["key_file"])])
    if "ca_file" in document["api"]:
        document["api"]["ca_file"] = str(retained_provider[Path(document["api"]["ca_file"])])
    for table in (document["uploads"], *document["interpreter"]["endpoints"]):
        if "ca_file" in table:
            table["ca_file"] = str(retained_provider[Path(table["ca_file"])])
    frozen = tomli_w.dumps(document).encode("utf-8")
    if len(frozen) + sum(map(len, consumed.values())) > maximum:
        raise ConfigurationRejected("the frozen deployment exceeds maximum_startup_bytes; no runtime profile was published.")
    path = bundle / "config/provider.toml"
    _write(path, frozen)

    def read_frozen(value, **limits):
        return read_startup_file(_source_path(value, bundle), **limits)

    config = _read_provider_config(path, read_frozen, read_api_file=read_frozen)
    capture_execution(source_root, bundle, source_names, **identities)
    # Repository declarations arrive as individual read-only source grants.
    # Checkout write modes are unrelated to the frozen config/secret policy.
    def source_bytes(value, maximum_bytes):
        with _source_path(value, source_root).open("rb") as source:
            raw = source.read(maximum_bytes + 1)
        if len(raw) > maximum_bytes:
            raise ConfigurationRejected("repository declaration exceeds its bounded source read.")
        return raw

    model_raw = source_bytes(models, 65536)
    try:
        _declared_models(read_document(model_raw, 65536))
        execution = read_document((bundle / "execution.json").read_bytes(), 65536)
    except ResponseRejected as cause:
        raise ConfigurationRejected("deployment execution/model declarations require strict JSON.", cause=cause) from None
    _write(bundle / "prediction-models.json", model_raw)
    for name in ("inference.lock", "unicore-source.json"):
        _write(bundle / name, source_bytes(source_root / "requirements" / name, 1024 * 1024))
    plan = render_compose(config, deployment=deployment, checkout=checkout, bundle=bundle,
                          uid=uid, gid=gid, prepared=prepared, execution=execution, routes=routes, grants=grants,
                          shared_image_id=shared_image_id)
    external = any(route["mode"] == "external" for route in local["routes"].values())
    if external:
        resolvers = local.get("resolvers")
        if type(resolvers) is not list or not resolvers or any(type(value) is not str for value in resolvers):
            raise ConfigurationRejected("external routes require explicit IPv4 resolvers in deployment.toml.")
        try:
            addresses = [ipaddress.IPv4Address(value) for value in resolvers]
        except ipaddress.AddressValueError:
            raise ConfigurationRejected("deployment resolvers require literal IPv4 addresses.") from None
        if len(set(addresses)) != len(addresses) or any(address.is_loopback for address in addresses):
            raise ConfigurationRejected("deployment resolvers must be distinct non-loopback IPv4 addresses reachable on the selected interface.")
        _write(bundle / "resolv.conf", "".join("nameserver " + str(address) + "\n" for address in addresses).encode())
    elif "resolvers" in local:
        raise ConfigurationRejected("resolvers are unused by local/mapped routes; remove that grant.")
    _write(bundle / "compose.json", compose_bytes(plan))
    seal_deployment_bundle(bundle, uid, gid)
    return bundle


def creation_plan(bundle):
    """Decode the public Compose recipe's literal-dollar encoding."""
    try:
        encoded = read_startup_file(_bind_path(Path(bundle) / "compose.json"), maximum_bytes=1024 * 1024,
                                    label="original Compose creation plan")
        return read_document(encoded.replace(b"$$", b"$"), 1024 * 1024)
    except ResponseRejected as cause:
        raise ConfigurationRejected("original Compose plan requires strict retained JSON.", cause=cause) from None


def seal_deployment_bundle(bundle, uid, gid):
    """Seal public recipe files and stage secrets as the provider's container identity."""
    bundle = _bind_path(bundle)
    if type(uid) is not int or type(gid) is not int or not 1 <= uid <= 4294967294 or not 1 <= gid <= 4294967294:
        raise ConfigurationRejected("deployment bundle sealing requires the admitted nonroot provider UID and GID.")
    root = bundle.lstat()
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != os.geteuid() or stat.S_IMODE(root.st_mode) != 0o700:
        raise ConfigurationRejected("deployment bundle sealing requires a caller-owned mode-0700 generation root.")
    rootless = os.geteuid() == 0
    if not rootless and os.geteuid() != uid:
        raise ConfigurationRejected("deployment helper must own its nonroot provider inputs before sealing.")
    directories, files = [], []
    for parent, names, leaves in os.walk(bundle, topdown=True, followlinks=False):
        directory = Path(parent)
        directories.append(directory)
        for name in names:
            path = directory / name
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ConfigurationRejected("deployment bundle directories must not contain links or non-directory entries.")
        for name in leaves:
            path = directory / name
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ConfigurationRejected("deployment bundle files must be regular files without symlinks.")
            files.append(path)
    if not any(path.relative_to(bundle).as_posix() == "compose.json" for path in files):
        raise ConfigurationRejected("deployment bundle has no retained Compose recipe to seal.")
    for path in files:
        mode = 0o400 if path.is_relative_to(bundle / "secrets") else 0o444
        os.chmod(path, mode, follow_symlinks=False)
        if rootless:
            os.chown(path, uid, gid, follow_symlinks=False)
        elif path.stat().st_uid != uid:
            raise ConfigurationRejected("deployment helper did not create provider-owned frozen inputs.")
    for directory in reversed(directories):
        if rootless:
            # Container root maps to the installing host user. Keeping directory
            # ownership there lets the host retire generations after service stop.
            os.chown(directory, 0, 0, follow_symlinks=False)
        elif directory.stat().st_uid != uid:
            raise ConfigurationRejected("deployment helper did not create provider-owned input directories.")
        os.chmod(directory, 0o555, follow_symlinks=False)


def lifetime_owner(bundle):
    """Select the existing producer grant, without deriving a second volume name."""
    provider = creation_plan(bundle)["services"]["provider"]
    grants = [grant for grant in provider["volumes"] if grant["target"] == "/srv/nmrnet/attempts"]
    if len(grants) != 1 or grants[0]["type"] != "volume" or grants[0]["read_only"]:
        raise ConfigurationRejected("the installed provider must have one writable provider lifetime volume grant.")
    uid, gid = provider["user"].split(":")
    return provider["image"], grants[0]["source"], uid, gid


def require_old_api_route(bundle, directory):
    """Admit only the private API directory named by the retained recipe."""
    plan = creation_plan(bundle)
    api = plan.get("services", {}).get("api", {})
    grants = [grant for grant in api.get("volumes", []) if grant.get("target") == "/route"]
    if (len(grants) != 1 or grants[0].get("type") != "bind"
            or grants[0].get("source") != str(directory) or grants[0].get("read_only") is not False):
        raise ConfigurationRejected("Cannot retire the old API route: the selected recipe does not grant its private directory to the API service.")


def initialize_lifetime(uid, gid):
    """Initialize only a fresh volume root; an existing producer owns its data."""
    if os.geteuid() != 0 or uid <= 0 or gid <= 0:
        raise ConfigurationRejected("provider lifetime initialization requires root with CHOWN and nonzero target UID/GID.")
    path = Path("/lifetime")
    observed = path.lstat()
    if not stat.S_ISDIR(observed.st_mode):
        raise ConfigurationRejected("the provider lifetime volume root must be a real directory.")
    if (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode)) == (uid, gid, 0o700):
        return
    if observed.st_uid != 0:
        raise ConfigurationRejected("refusing to change a provider lifetime volume with unexpected ownership or mode.")
    with os.scandir(path) as entries:
        if next(entries, None) is not None:
            raise ConfigurationRejected("refusing to change a nonempty provider lifetime volume with unexpected ownership or mode.")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(descriptor, 0o700)
        os.fchown(descriptor, uid, gid)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def prepare_runtime(runtime_root, uid, gid, *, check_only=False):
    """Validate or give each mounted runtime leaf its mapped service identity."""
    if type(uid) is not int or type(gid) is not int or not 1 <= uid <= 4294967294 or not 1 <= gid <= 4294967294:
        raise ConfigurationRejected("provider runtime ownership requires nonzero mapped service UID and GID values.")
    runtime_root = _bind_path(runtime_root)
    root = runtime_root.lstat()
    if not stat.S_ISDIR(root.st_mode) or root.st_uid != os.geteuid() or stat.S_IMODE(root.st_mode) != 0o700:
        raise ConfigurationRejected("provider runtime root must remain caller-owned and private.")
    expected = {
        "prediction": {".owner-session.lock": stat.S_ISREG},
        "reader": {".owner-session.lock": stat.S_ISREG},
        "smiles": {".owner-session.lock": stat.S_ISREG},
        "route-interpreter": {"proxy.sock": stat.S_ISSOCK, "ready": stat.S_ISREG},
        "route-store": {"proxy.sock": stat.S_ISSOCK, "ready": stat.S_ISREG},
    }
    ownership_targets = []
    missing_locks = []
    for name, leaves in expected.items():
        directory = runtime_root / name
        observed = directory.lstat()
        if (not stat.S_ISDIR(observed.st_mode)
                or observed.st_uid not in {os.geteuid(), uid}
                or stat.S_IMODE(observed.st_mode) != 0o700):
            raise ConfigurationRejected("provider runtime directories must be private directories owned by the caller or mapped service user.")
        with os.scandir(directory) as entries:
            actual = {entry.name: entry for entry in entries}
        if set(actual) - set(leaves):
            raise ConfigurationRejected("provider runtime directory contains an unowned entry; existing contents were preserved.")
        for leaf, is_type in leaves.items():
            entry = actual.get(leaf)
            if entry is None:
                if leaf == ".owner-session.lock":
                    missing_locks.append(directory / leaf)
                continue
            path = directory / leaf
            item = path.lstat()
            if item.st_nlink != 1:
                relative = f"{name}/{leaf}"
                raise ConfigurationRejected(
                    f"provider runtime entry {relative} has another hard link; refusing ownership change because it would change that alias too."
                )
            if (not is_type(item.st_mode) or item.st_uid not in {os.geteuid(), uid}
                    or stat.S_IMODE(item.st_mode) != 0o600):
                raise ConfigurationRejected("provider runtime files must be private, standalone regular files or sockets owned by the caller or mapped service user.")
            ownership_targets.append(path)
        ownership_targets.append(directory)
    if check_only:
        return
    for path in missing_locks:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        ownership_targets.append(path)
    for path in ownership_targets:
        os.chown(path, uid, gid, follow_symlinks=False)


def client_inputs(*, bundle, prediction_image_id, client_root):
    """Export nonsecret client expectations from the admitted running generation.

    Host inventory supplies the actual image pin; historical numerical receipts
    keep their original execution metadata and never supply this current fact.
    """
    plan = creation_plan(bundle)
    if plan["services"]["prediction"]["image"] != prediction_image_id:
        raise ConfigurationRejected("the running prediction image differs from its admitted bundle.")
    configuration = bundle / "config/provider.toml"
    raw = read_startup_file(configuration, maximum_bytes=65536, label="frozen provider configuration")

    def read_file(path, **limits):
        if Path(path) == configuration:
            return raw
        return read_startup_file(path, **limits)

    admitted = _read_provider_config(configuration, read_file, read_api_file=read_file)
    _disjoint(client_root, bundle.parent.parent)
    identity = {
        "origin": admitted.api.identity.origin, "authority": admitted.api.identity.authority,
        "profile": tomllib.loads(raw.decode())["api"]["profile"],
        "topology": admitted.api.expected_topology, "provider_ref": admitted.lifecycle.scope.provider_ref,
        "interpretation_scripted": False,
        "interpreter": {"max_turns": admitted.interpreter.max_turns, "endpoints": [
            json.loads(ChatEndpoint(endpoint.spec, transport=None, request_bytes=endpoint.request_bytes,
                                    response_bytes=endpoint.response_bytes).identity)
            for endpoint in admitted.interpreter.endpoints
        ]},
        "prediction_image_id": prediction_image_id, "bundle": bundle.name,
    }
    sys.stdout.write(json.dumps(identity, sort_keys=True) + "\n")


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Prepare deployment configuration and export public-client expectations offline.")
    operations = parser.add_subparsers(dest="operation", required=True)
    freeze = operations.add_parser("freeze", help="Freeze one deployment generation without starting services.")
    for name in ("definition", "settings", "secrets", "bundle", "source-root", "models", "checkout", "prepared"):
        freeze.add_argument("--" + name, type=Path, required=True)
    freeze.add_argument("--source", dest="source_names", action="append", required=True)
    freeze.add_argument("--deployment", required=True)
    freeze.add_argument("--shared-image-id", required=True)
    for name in ("uid", "gid"):
        freeze.add_argument("--" + name, type=int, required=True)
    for name in ("image-id", "geometry-image-id", "controller-image-id", "repository-revision", "upstream-revision"):
        freeze.add_argument("--" + name, required=True)
    client = operations.add_parser("client-inputs", help="Export current nonsecret expectations for one public-client witness.")
    client.add_argument("--bundle", type=Path, required=True)
    client.add_argument("--prediction-image-id", required=True)
    client.add_argument("--client-root", type=Path, required=True)
    lifetime = operations.add_parser("lifetime-owner", help="Print the installed provider lifetime grant and its image/owner.")
    lifetime.add_argument("--bundle", type=Path, required=True)
    route = operations.add_parser("old-api-route", help="Admit the old API directory from its retained recipe.")
    route.add_argument("--bundle", type=Path, required=True)
    route.add_argument("--directory", type=Path, required=True)
    initialize = operations.add_parser("initialize-lifetime", help="Initialize a fresh mounted provider lifetime volume.")
    initialize.add_argument("--uid", type=int, required=True)
    initialize.add_argument("--gid", type=int, required=True)
    runtime = operations.add_parser("prepare-runtime", help="Grant private runtime leaves to the mapped container service user.")
    runtime.add_argument("--runtime-root", type=Path, required=True)
    runtime.add_argument("--uid", type=int, required=True)
    runtime.add_argument("--gid", type=int, required=True)
    runtime.add_argument("--check-only", action="store_true")
    options = parser.parse_args(arguments)
    values = vars(options)
    operation = values.pop("operation")
    try:
        if operation == "freeze":
            freeze_deployment(**values)
        elif operation == "client-inputs":
            client_inputs(**values)
            return 0
        elif operation == "lifetime-owner":
            sys.stdout.write(" ".join(lifetime_owner(**values)) + "\n")
            return 0
        elif operation == "old-api-route":
            require_old_api_route(**values)
            return 0
        elif operation == "initialize-lifetime":
            initialize_lifetime(**values)
            sys.stdout.write("Provider lifetime volume ready.\n")
            return 0
        elif operation == "prepare-runtime":
            prepare_runtime(**values)
            return 0
    except (ConfigurationRejected, OSError) as failure:
        # Protected Docker-stderr projection for this finite receiving boundary:
        # preserve owned causal evidence without arbitrary exception stringify.
        sys.stderr.write(json.dumps(failure_evidence(failure, secrets=())) + "\n")
        return 2
    sys.stdout.write("Deployment configuration prepared offline; no API/model request or service start occurred.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
