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


def stage(root, origin, topology, *, inputs, interpreter_config):
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
    }
    if interpreter_config is not None:
        endpoint_settings = admitted.interpreter.endpoints[0]
        identity["interpreter"] = json.loads(
            ChatEndpoint(
                endpoint_settings.spec,
                transport=endpoint_settings.http.new_transport(),
                request_bytes=endpoint_settings.request_bytes,
                response_bytes=endpoint_settings.response_bytes,
            ).identity
        )
    (source / "identity.json").write_text(json.dumps(identity) + "\n")
    (source / "host.txt").write_text(host + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--origin")
    parser.add_argument("--topology")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--inspect-interpreter", type=Path)
    selection.add_argument("--interpreter-config", type=Path)
    args = parser.parse_args()
    if args.inspect_interpreter is not None:
        if args.origin is not None or args.topology is not None:
            parser.error("interpreter file inspection does not accept API origin or topology")
        inspect_interpreter(args.inspect_interpreter, args.root)
    else:
        if args.origin is None or args.topology is None:
            parser.error("configuration staging requires explicit --origin and --topology")
        stage(args.root, args.origin, args.topology, inputs=Path("/input"), interpreter_config=args.interpreter_config)
