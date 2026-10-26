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
import hashlib
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
from provider_core.deployment_binding import BindingDefinitionRejected, load_deployment_bindings

INPUT_ROOT = Path("/srv/nmrnet/input")


def _write(path, raw):
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def freeze_deployment(definition, settings, secrets, bundle, source_root, models,
                      *, source_names, deployment, checkout, uid, gid, prepared,
                      delivery_bindings, delivery_binding, shared_image_id, **identities):
    """Freeze admitted inputs into a fresh generation for Compose normalization.

    No API/model request, store opening or worker allocation occurs. Failed
    generations retain their actual configuration, and previous generations stay intact.
    The caller receives the resulting Compose configuration directly.
    """
    definition, settings, secrets, bundle, source_root, models = map(
        _bind_path, (definition, settings, secrets, bundle, source_root, models)
    )
    if any(bundle.iterdir()):
        raise ConfigurationRejected("choose a fresh empty deployment bundle; existing generations were preserved.")
    raw = read_startup_file(definition, maximum_bytes=65536, label="public deployment definition")
    local_raw = read_startup_file(settings, maximum_bytes=65536, label="private deployment settings", secret=True)
    try:
        document = tomllib.loads(raw.decode("utf-8"))
        local = tomllib.loads(local_raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise ConfigurationRejected("provider and deployment definitions must contain valid UTF-8 TOML.") from None
    try:
        binding = load_deployment_bindings(_bind_path(delivery_bindings)).selected(delivery_binding)
    except BindingDefinitionRejected as failure:
        raise ConfigurationRejected(f"Cannot select shared delivery binding: {failure}", cause=failure) from None
    if "delivery" not in document or type(document["delivery"]) is not dict:
        raise ConfigurationRejected("provider definition requires a delivery table with its IPC intake policy.")
    if "api" in document or {"socket_path", "socket_group_id"} & document["delivery"].keys():
        raise ConfigurationRejected("API settings and delivery socket grants must come only from the selected shared binding.")
    document["api"] = dict(binding.api)
    document["delivery"].update(socket_path=str(binding.socket_path), socket_group_id=binding.socket_group_id)
    raw = tomli_w.dumps(document).encode("utf-8")
    if not {"maximum_startup_bytes", "routes"} <= set(local) or set(local) - {
        "maximum_startup_bytes", "routes", "resolvers"
    }:
        raise ConfigurationRejected("deployment.toml requires maximum_startup_bytes and routes; only external routing accepts resolvers.")
    maximum = local["maximum_startup_bytes"]
    if type(maximum) is not int or maximum < 1:
        raise ConfigurationRejected("deployment maximum_startup_bytes must be a positive integer.")
    if type(local["routes"]) is not dict or set(local["routes"]) != {"interpreter", "store"}:
        raise ConfigurationRejected("deployment.toml owns only interpreter/store routes; the selected shared binding owns the API route.")
    routes = dict(local["routes"])
    routes["api"] = {key: value for key, value in binding.route.items() if key not in {"resolver_file", "network_id"}}
    if binding.route["mode"] == "local":
        routes["api"]["network"] = binding.route["network_id"]
    (bundle / "config").mkdir(mode=0o700)
    (bundle / "secrets").mkdir(mode=0o700)
    for role in ("api", "provider"):
        (bundle / "secrets" / role).mkdir(mode=0o700)
    consumed, retained_api, retained_provider = {}, {}, {}
    api_inputs = {Path(binding.api["credential_file"]): Path("api/signing.json")}
    if "ca_file" in binding.api:
        api_inputs[Path(binding.api["ca_file"])] = Path("api/ca.pem")

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

    def retain_api(path, **limits):
        path = _bind_path(path)
        leaf = api_inputs[path]
        retained_api[path] = bundle / "secrets" / leaf
        return retain_input(path, leaf, **limits)

    admitted = _read_provider_config(definition, retain_provider, read_api_file=retain_api)
    if Path(admitted.lifecycle.store_root) != Path("/srv/nmrnet/attempts"):
        raise ConfigurationRejected(
            "the public deployment store_root must be /srv/nmrnet/attempts, the producer checkpoint volume's container path."
        )
    # Only admitted input paths change; the writer preserves the parsed scientific
    # values without imposing scalar-line or table-layout restrictions on TOML.
    document["api"]["credential_file"] = str(retained_api[Path(document["api"]["credential_file"])])
    for endpoint in document["interpreter"]["endpoints"]:
        endpoint["key_file"] = str(retained_provider[Path(endpoint["key_file"])])
    if "ca_file" in document["api"]:
        document["api"]["ca_file"] = str(retained_api[Path(document["api"]["ca_file"])])
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
                          uid=uid, gid=gid, prepared=prepared, execution=execution, routes=routes,
                          delivery_project=binding.project, shared_image_id=shared_image_id)
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
    if binding.route["mode"] == "external":
        _write(bundle / "api-resolv.conf", read_startup_file(
            _bind_path(binding.route["resolver_file"]), maximum_bytes=65536, label="selected API resolver definition"
        ))
    _write(bundle / "compose.json", compose_bytes(plan))
    return bundle


def creation_plan(bundle):
    """Decode the recipe owner's literal-dollar encoding without evaluating it."""
    try:
        encoded = read_startup_file(_bind_path(Path(bundle) / "compose.json"), maximum_bytes=1024 * 1024,
                                    label="original Compose creation plan", secret=True)
        return read_document(encoded.replace(b"$$", b"$"), 1024 * 1024)
    except ResponseRejected as cause:
        raise ConfigurationRejected("original Compose plan requires strict retained JSON.", cause=cause) from None


def checkpoint_owner(bundle):
    """Select the existing producer grant, without deriving a second volume name."""
    provider = creation_plan(bundle)["services"]["provider"]
    grants = [grant for grant in provider["volumes"] if grant["target"] == "/srv/nmrnet/attempts"]
    if len(grants) != 1 or grants[0]["type"] != "volume" or grants[0]["read_only"]:
        raise ConfigurationRejected("the installed provider must have one writable checkpoint volume grant.")
    uid, gid = provider["user"].split(":")
    return provider["image"], grants[0]["source"], uid, gid


def initialize_checkpoint(uid, gid):
    """Initialize only a fresh volume root; an existing producer owns its data."""
    if os.geteuid() != 0 or uid <= 0 or gid <= 0:
        raise ConfigurationRejected("checkpoint initialization requires root with CHOWN and nonzero target UID/GID.")
    path = Path("/checkpoint")
    observed = path.lstat()
    if not stat.S_ISDIR(observed.st_mode):
        raise ConfigurationRejected("the checkpoint volume root must be a real directory.")
    if (observed.st_uid, observed.st_gid, stat.S_IMODE(observed.st_mode)) == (uid, gid, 0o700):
        return
    if observed.st_uid != 0:
        raise ConfigurationRejected("refusing to change a checkpoint volume with unexpected ownership or mode.")
    with os.scandir(path) as entries:
        if next(entries, None) is not None:
            raise ConfigurationRejected("refusing to change a nonempty checkpoint volume with unexpected ownership or mode.")
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fchmod(descriptor, 0o700)
        os.fchown(descriptor, uid, gid)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    for name in ("definition", "settings", "secrets", "bundle", "source-root", "models", "checkout", "prepared", "delivery-bindings"):
        freeze.add_argument("--" + name, type=Path, required=True)
    freeze.add_argument("--source", dest="source_names", action="append", required=True)
    freeze.add_argument("--deployment", required=True)
    freeze.add_argument("--delivery-binding", required=True)
    freeze.add_argument("--shared-image-id", required=True)
    for name in ("uid", "gid"):
        freeze.add_argument("--" + name, type=int, required=True)
    for name in ("image-id", "geometry-image-id", "controller-image-id", "repository-revision", "upstream-revision"):
        freeze.add_argument("--" + name, required=True)
    client = operations.add_parser("client-inputs", help="Export current nonsecret expectations for one public-client witness.")
    client.add_argument("--bundle", type=Path, required=True)
    client.add_argument("--prediction-image-id", required=True)
    client.add_argument("--client-root", type=Path, required=True)
    checkpoint = operations.add_parser("checkpoint-owner", help="Print the installed checkpoint grant and its image/owner.")
    checkpoint.add_argument("--bundle", type=Path, required=True)
    initialize = operations.add_parser("initialize-checkpoint", help="Initialize a fresh mounted producer checkpoint volume.")
    initialize.add_argument("--uid", type=int, required=True)
    initialize.add_argument("--gid", type=int, required=True)
    options = parser.parse_args(arguments)
    values = vars(options)
    operation = values.pop("operation")
    try:
        if operation == "freeze":
            freeze_deployment(**values)
        elif operation == "client-inputs":
            client_inputs(**values)
            return 0
        elif operation == "checkpoint-owner":
            sys.stdout.write(" ".join(checkpoint_owner(**values)) + "\n")
            return 0
        elif operation == "initialize-checkpoint":
            initialize_checkpoint(**values)
            sys.stdout.write("Producer checkpoint volume ready.\n")
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
