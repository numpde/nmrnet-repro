"""Materialize the first actual API witness using the existing closed TOML fixture.

ConfigFiles supplies explicit budgets, not production defaults. Replace its
disposable provider identity with the original API-issued bytes before admission.
The model peer gets a separate grant; it cannot read this configuration or key.
"""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import tomllib
from config_ports import ConfigFiles

from nmrnet_provider.config import _interpreter, load_provider_config, read_startup_file
from nmrnet_provider.startup_capture import _bind_path
from provider_core.chat import ChatEndpoint
from provider_core.credential import parse_provider_credential
from tools.provider_proxy import authority_parts


def interpreter_fragment(path):
    """Read one private fragment and identify the exact files needed for admission.

    This first pass receives no key grant. The existing interpreter owner admits
    endpoint semantics after the host grants the referenced individual files.
    """
    raw = read_startup_file(path, maximum_bytes=65536, label="interpreter configuration", secret=True)
    document = tomllib.loads(raw.decode("utf-8"))
    if set(document) != {"interpreter"}:
        raise ValueError("Cannot stage interpreter configuration: the fragment must contain only its interpreter table")
    table = document["interpreter"]
    if type(table) is not dict or type(table.get("endpoints")) is not list or len(table["endpoints"]) != 1:
        raise ValueError("Cannot stage interpreter configuration: this qualification requires exactly one endpoint")
    endpoint = table["endpoints"][0]
    if type(endpoint) is not dict or "key_file" not in endpoint:
        raise ValueError("Cannot stage interpreter configuration: its endpoint must name a separate key file")
    inputs = {
        "configuration_sha256": hashlib.sha256(raw).hexdigest(),
        "key_file": str(_bind_path(endpoint["key_file"])),
        "ca_file": str(_bind_path(endpoint["ca_file"])) if "ca_file" in endpoint else None,
    }
    return table, inputs


def inspect_interpreter(path, output):
    """Publish private file selectors without reading a referenced credential."""
    os.umask(0o077)
    _table, inputs = interpreter_fragment(path)
    for name in ("key_file", "ca_file"):
        with (output / (name + ".txt")).open("x") as stream:
            stream.write((inputs[name] or "") + "\n")
    with (output / "interpreter-inputs.json").open("x") as stream:
        stream.write(json.dumps(inputs) + "\n")


def stage_deployment(root, *, api_interface, api_address, interpreter_route,
                     interpreter_interface, interpreter_network, interpreter_address, resolvers):
    """Translate this admitted fixture into the production named-deployment inputs.

    Keep the original API-issued key bytes. Only the
    fixture's known file paths change; the deployment owner admits route/grant
    semantics before it can start anything or the client can submit its one Job.
    """
    os.umask(0o077)
    source, destination = root / "source", root / "deployment"
    if any(destination.iterdir()):
        raise ValueError("Deployment staging requires an empty private output grant")
    secrets = destination / "secrets"
    secrets.mkdir(mode=0o700)
    raw = (source / "provider.toml").read_text()
    for name in ("signing.private.json", "interpreter.key", "api-ca.crt", "api-ca.pem", "model-ca.pem"):
        path = source / name
        # A public-CA interpreter has no model-ca file; staging already admitted
        # that endpoint. Never substitute a CA when one was actually configured.
        if path.exists():
            with (secrets / name).open("xb") as output:
                output.write(path.read_bytes())
            raw = raw.replace(json.dumps(str(path)), json.dumps("/srv/nmrnet/input/" + name))
    raw = raw.replace(json.dumps(str(root / "state")), json.dumps("/srv/nmrnet/attempts"))
    (destination / "provider.toml").write_text(raw)
    document = tomllib.loads(raw)
    host = urlsplit(document["api"]["origin"]).hostname
    mapped = {"mode": "mapped", "interface": api_interface, "addresses": [host + "=" + api_address]}
    interpreter = {"mode": interpreter_route, "interface": interpreter_interface}
    if interpreter_route == "local":
        interpreter.update(network=interpreter_network, addresses=["localhost=" + interpreter_address])
    elif interpreter_route == "mapped":
        model_host = urlsplit(document["interpreter"]["endpoints"][0]["url"]).hostname
        interpreter["addresses"] = [model_host + "=" + interpreter_address]
    elif interpreter_route != "external":
        raise ValueError("Qualification interpretation requires an explicit local, mapped or external route")
    lines = ["maximum_startup_bytes = 2097152"]
    if interpreter_route == "external":
        lines.append("resolvers = " + json.dumps(resolvers))
    for role, route in (("api", mapped), ("interpreter", interpreter), ("store", mapped)):
        lines.append("\n[routes." + role + "]")
        lines.extend(name + " = " + json.dumps(value) for name, value in route.items())
    (destination / "deployment.toml").write_text("\n".join(lines) + "\n")


def stage(root, origin, topology, *, inputs, interpreter_config, prediction_image_id):
    if re.fullmatch(r"sha256:[0-9a-f]{64}", prediction_image_id) is None:
        raise ValueError("Qualification requires the explicit current prediction image identity.")
    """Freeze one qualification's inputs before normal provider startup capture."""
    os.umask(0o077)
    source = root / "source"
    if any(source.iterdir()):
        raise ValueError("Configuration staging requires an empty private source grant")
    api_ca, model_ca = source / "api-ca.crt", source / "model-ca.pem"
    api_ca.write_bytes((inputs / "api-ca.crt").read_bytes())
    if interpreter_config is None:
        model_ca.write_bytes((inputs / "model-ca.pem").read_bytes())
        model_origin = json.loads((inputs / "model-ready.json").read_bytes())["origin"]
    else:
        table, referenced = interpreter_fragment(interpreter_config)
        if referenced != json.loads((inputs / "interpreter-inputs.json").read_bytes()):
            raise ValueError("Cannot stage interpreter configuration: it changed after its individual file grants were selected")
        captured_inputs = {}
        granted_paths = {referenced["key_file"]: inputs / "interpreter.key"}
        if referenced["ca_file"] is not None:
            granted_paths[referenced["ca_file"]] = inputs / "interpreter-ca.pem"

        def retain(value, **settings):
            raw = read_startup_file(granted_paths[str(value)], **settings)
            captured_inputs[str(value)] = raw
            return raw

        selected = _interpreter(table, retain)
        model_url = urlsplit(selected.endpoints[0].spec.url)
        model_origin = f"{model_url.scheme}://{model_url.netloc}"
    original = read_startup_file(inputs / "provider.json", maximum_bytes=16384, label="provider credential", secret=True)
    credential = parse_provider_credential(original)
    files = ConfigFiles(
        source,
        api_origin=origin,
        store_origin=origin,
        model_origin=model_origin,
        ca_file=api_ca,
        model_ca_file=model_ca,
        store_ca_file=api_ca,
        # Keep the completed Job's durable record plus one free admission slot.
        # A full store correctly refuses new work, including the next idle poll.
        maximum_records=2,
    )
    files.credential.write_bytes(original)
    raw = files.raw.replace(
        b'provider_ref = "provider:example"', ("provider_ref = " + json.dumps(credential.provider_ref)).encode()
    )
    raw = raw.replace(b'profile = "dev-local"', ("profile = " + json.dumps(credential.profile)).encode())
    raw = raw.replace(b'topology = "dev-local"', ("topology = " + json.dumps(topology)).encode())
    presentation = tomllib.loads(Path("/fixture/provider.toml.example").read_text())["service"]
    for field, previous in (("description", "NMRNet coordinate shifts"), ("offering_description", "Coordinate shifts")):
        raw = raw.replace(
            (field + " = " + json.dumps(previous)).encode(),
            (field + " = " + json.dumps(presentation[field])).encode(),
        )
    raw = raw.replace(str(files.state).encode(), str(root / "state").encode())
    if interpreter_config is not None:
        files.key.write_bytes(captured_inputs[referenced["key_file"]])
        endpoint = dict(table["endpoints"][0], key_file=str(files.key))
        if referenced["ca_file"] is not None:
            model_ca.write_bytes(captured_inputs[referenced["ca_file"]])
            endpoint["ca_file"] = str(model_ca)
        # Endpoint fields have already passed the existing closed configuration
        # owner. Re-emit only this admitted table with its frozen local file paths.
        fragment = "[interpreter]\nmax_turns = " + str(table["max_turns"]) + "\n\n[[interpreter.endpoints]]\n"
        fragment += "".join(name + " = " + json.dumps(value, ensure_ascii=False) + "\n" for name, value in endpoint.items())
        prefix, remaining = raw.split(b"[interpreter]\n", 1)
        _old, suffix = remaining.split(b"[uploads]\n", 1)
        raw = prefix + fragment.encode() + b"\n[uploads]\n" + suffix
        raw = raw.replace(b"\nseconds = 45\n", b"\nseconds = 240\n")
        # The Upload session starts during Attempt assembly, before model turns:
        # allow 240 seconds for preparation plus a finite 60-second setup margin.
        raw = raw.replace(b"\nseconds = 10\n", b"\nseconds = 300\n")
    files.write(raw)
    admitted = load_provider_config(files.path)
    parts = urlsplit(admitted.api.identity.origin)
    host, _port = authority_parts(f"{parts.hostname}:{parts.port if parts.port is not None else 443}")
    # Nonsecret facts from the exact admitted configuration, shared with the
    # public client. The key and raw model/configuration text stay separate.
    identity = {
        "origin": admitted.api.identity.origin,
        "authority": admitted.api.identity.authority,
        "provider_ref": admitted.lifecycle.scope.provider_ref,
        "profile": credential.profile,
        "topology": admitted.api.expected_topology,
        "interpretation_scripted": interpreter_config is None,
        "prediction_image_id": prediction_image_id,
    }
    if interpreter_config is not None:
        endpoint_settings = admitted.interpreter.endpoints[0]
        identity["interpreter"] = {"max_turns": admitted.interpreter.max_turns, "endpoints": [json.loads(
            ChatEndpoint(
                endpoint_settings.spec,
                transport=endpoint_settings.http.new_transport(),
                request_bytes=endpoint_settings.request_bytes,
                response_bytes=endpoint_settings.response_bytes,
            ).identity
        )]}
    (source / "identity.json").write_text(json.dumps(identity) + "\n")
    (source / "host.txt").write_text(host + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--origin")
    parser.add_argument("--topology")
    parser.add_argument("--prediction-image-id")
    parser.add_argument("--deployment", action="store_true")
    parser.add_argument("--api-interface")
    parser.add_argument("--api-address")
    parser.add_argument("--interpreter-route", choices=("local", "external"))
    parser.add_argument("--interpreter-interface")
    parser.add_argument("--interpreter-network")
    parser.add_argument("--interpreter-address")
    parser.add_argument("--resolver", action="append", default=[])
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--inspect-interpreter", type=Path)
    selection.add_argument("--interpreter-config", type=Path)
    args = parser.parse_args()
    if args.inspect_interpreter is not None:
        if args.origin is not None or args.topology is not None:
            parser.error("interpreter file inspection does not accept API origin or topology")
        inspect_interpreter(args.inspect_interpreter, args.root)
    else:
        if args.origin is None or args.topology is None or args.prediction_image_id is None:
            parser.error("configuration staging requires explicit --origin, --topology and --prediction-image-id")
        stage(args.root, args.origin, args.topology, inputs=Path("/input"), interpreter_config=args.interpreter_config,
              prediction_image_id=args.prediction_image_id)
        if args.deployment:
            stage_deployment(
                args.root, api_interface=args.api_interface, api_address=args.api_address,
                interpreter_route=args.interpreter_route, interpreter_interface=args.interpreter_interface,
                interpreter_network=args.interpreter_network, interpreter_address=args.interpreter_address,
                resolvers=args.resolver,
            )
