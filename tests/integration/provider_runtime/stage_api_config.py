"""Materialize the first actual API witness using the existing closed TOML fixture.

ConfigFiles supplies explicit budgets, not production defaults. Replace its
disposable provider identity with the original API-issued bytes before admission.
The model peer gets a separate grant; it cannot read this configuration or key.
"""

from tools.container_guard import require_container

require_container()

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from config_ports import ConfigFiles

from nmrnet_provider.config import load_provider_config, read_startup_file
from provider_core.credential import parse_provider_credential
from tools.provider_proxy import authority_parts


def stage(root, origin, topology):
    os.umask(0o077)
    source = root / "source"
    if any(source.iterdir()):
        raise ValueError("Configuration staging requires an empty private source grant")
    api_ca, model_ca = source / "api-ca.crt", source / "model-ca.pem"
    api_ca.write_bytes(Path("/input/api-ca.crt").read_bytes())
    model_ca.write_bytes(Path("/input/model-ca.pem").read_bytes())
    model = json.loads(Path("/input/model-ready.json").read_bytes())
    original = read_startup_file(Path("/input/provider.json"), maximum_bytes=16384, label="provider credential", secret=True)
    credential = parse_provider_credential(original)
    files = ConfigFiles(
        source,
        api_origin=origin,
        store_origin=origin,
        model_origin=model["origin"],
        ca_file=api_ca,
        model_ca_file=model_ca,
        store_ca_file=api_ca,
    )
    files.credential.write_bytes(original)
    raw = files.raw.replace(
        b'provider_ref = "provider:example"', ("provider_ref = " + json.dumps(credential.provider_ref)).encode()
    )
    raw = raw.replace(b'profile = "dev-local"', ("profile = " + json.dumps(credential.profile)).encode())
    raw = raw.replace(b'topology = "dev-local"', ("topology = " + json.dumps(topology)).encode())
    raw = raw.replace(str(files.state).encode(), str(root / "state").encode())
    files.write(raw)
    admitted = load_provider_config(files.path)
    parts = urlsplit(admitted.api.identity.origin)
    host, _port = authority_parts(f"{parts.hostname}:{parts.port if parts.port is not None else 443}")
    # Nonsecret facts from the exact admitted configuration, shared with the
    # public client. The key and raw model/configuration text stay separate.
    (source / "identity.json").write_text(
        json.dumps(
            {
                "origin": admitted.api.identity.origin,
                "authority": admitted.api.identity.authority,
                "provider_ref": admitted.lifecycle.scope.provider_ref,
                "profile": credential.profile,
                "topology": admitted.api.expected_topology,
            }
        )
        + "\n"
    )
    (source / "host.txt").write_text(host + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--topology", required=True)
    args = parser.parse_args()
    stage(args.root, args.origin, args.topology)
