"""Materialize qualification configuration and deployment inputs."""

from tools.container_guard import require_container

require_container()

import json
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

from binding_inputs import write_deployment_inputs
from config_ports import ConfigFiles
from interpreter_fragment import interpreter_fragment

from nmrnet_provider.config import load_provider_config, read_startup_file
from provider_core.chat import ChatEndpoint
from provider_core.credential import parse_provider_credential
from provider_core.provider_proxy import authority_parts
from provider_core.shared_grants import shared_grants


def stage_deployment(
    root,
    *,
    api_interface,
    api_address,
    interpreter_route,
    interpreter_interface,
    interpreter_network,
    interpreter_address,
    resolvers,
    deployment_settings,
):
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
    endpoint_text = (source / "interpreter.d/00-primary.toml").read_text()
    for name in ("signing.private.json", "interpreter.key", "api-ca.crt", "api-ca.pem", "model-ca.pem"):
        path = source / name
        # A public-CA interpreter has no model-ca file; staging already admitted
        # that endpoint. Never substitute a CA when one was actually configured.
        if path.exists():
            with (secrets / name).open("xb") as output:
                output.write(path.read_bytes())
            raw = raw.replace(json.dumps(str(path)), json.dumps("/srv/nmrnet/input/" + name))
            endpoint_text = endpoint_text.replace(json.dumps(str(path)), json.dumps("/srv/nmrnet/input/" + name))
    raw = raw.replace(json.dumps(str(root / "state")), json.dumps("/srv/nmrnet/attempts"))
    document = tomllib.loads(raw)
    host = urlsplit(document["api"]["origin"]).hostname
    mapped = {"mode": "mapped", "interface": api_interface, "log_level": "INFO", "addresses": [host + "=" + api_address]}
    interpreter = {"mode": interpreter_route, "interface": interpreter_interface, "log_level": "INFO"}
    if interpreter_route == "local":
        interpreter.update(network=interpreter_network, addresses=["localhost=" + interpreter_address])
    elif interpreter_route == "mapped":
        model_host = urlsplit(tomllib.loads(endpoint_text)["url"]).hostname
        interpreter["addresses"] = [model_host + "=" + interpreter_address]
    elif interpreter_route != "external":
        raise ValueError("Qualification interpretation requires an explicit local, mapped or external route")
    api = dict(document["api"])
    for name in ("credential_file", "ca_file"):
        if name in api:
            api[name] = str(secrets / Path(api[name]).name)
    grants = shared_grants(deployment_settings)
    write_deployment_inputs(
        destination,
        document,
        api=api,
        api_route=mapped,
        routes={"interpreter": interpreter, "store": mapped},
        project=grants.project,
        gid=grants.gid,
        maximum_startup_bytes=2097152,
        resolvers=resolvers if interpreter_route == "external" else [],
    )
    endpoint_directory = destination / "interpreter.d"
    endpoint_directory.mkdir(mode=0o700)
    (endpoint_directory / "00-primary.toml").write_text(endpoint_text)


def stage(root, origin, topology, *, inputs, interpreter_config, prediction_image_id):
    """Freeze one qualification's inputs before normal provider startup capture."""
    if re.fullmatch(r"sha256:[0-9a-f]{64}", prediction_image_id) is None:
        raise ValueError("Qualification requires the explicit current prediction image identity.")
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

        retain(referenced["key_file"], maximum_bytes=16384, label="interpreter key", secret=True)
        if referenced["ca_file"] is not None:
            retain(referenced["ca_file"], maximum_bytes=1048576, label="interpreter CA")
        model_url = urlsplit(table["url"])
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
        maximum_live_attempts=2,
    )
    files.credential.write_bytes(original)
    raw = files.raw.replace(
        b'provider_ref = "provider:example"', ("provider_ref = " + json.dumps(credential.provider_ref)).encode()
    )
    raw = raw.replace(b'profile = "dev-local"', ("profile = " + json.dumps(credential.profile)).encode())
    raw = raw.replace(b'topology = "dev-local"', ("topology = " + json.dumps(topology)).encode())
    presentation = tomllib.loads(Path("/fixture/provider.toml.example").read_text())["service"]
    raw = raw.replace(
        b'description = "NMRNet coordinate shifts"',
        ("description = " + json.dumps(presentation["description"])).encode(),
    )
    raw = raw.replace(str(files.state).encode(), str(root / "state").encode())
    if interpreter_config is not None:
        files.key.write_bytes(captured_inputs[referenced["key_file"]])
        endpoint = dict(table, key_file=str(files.key))
        if referenced["ca_file"] is not None:
            model_ca.write_bytes(captured_inputs[referenced["ca_file"]])
            endpoint["ca_file"] = str(model_ca)
        # Endpoint fields have already passed the existing closed configuration
        # owner. Re-emit only this admitted table with its frozen local file paths.
        files.endpoint.write_text(
            "".join(name + " = " + json.dumps(value, ensure_ascii=False) + "\n" for name, value in endpoint.items())
        )
        raw = raw.replace(b"timeout_seconds = 6", b"timeout_seconds = 240")
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
        identity["interpreter"] = {
            "timeout_seconds": admitted.interpreter.timeout_seconds,
            "endpoints": [
                json.loads(
                    ChatEndpoint(
                        endpoint_settings.spec,
                        transport=None,
                        request_bytes=endpoint_settings.request_bytes,
                        response_bytes=endpoint_settings.response_bytes,
                    ).identity
                )
            ],
        }
    (source / "identity.json").write_text(json.dumps(identity) + "\n")
    (source / "host.txt").write_text(host + "\n")
