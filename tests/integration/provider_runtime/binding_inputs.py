"""Author isolated qualification inputs for the normal provider and shared owners."""

import os
from pathlib import Path
from urllib.parse import urlsplit

import tomli_w


def write_deployment_inputs(directory, document, *, api, api_route, routes, project, gid,
                            maximum_startup_bytes, resolvers, shared_fixture=False):
    """Keep provider identity private and optionally author a disposable host."""
    document.pop("api")
    document["delivery"].pop("socket_path")
    document["delivery"].pop("socket_group_id")
    settings = {"maximum_startup_bytes": maximum_startup_bytes,
                "deployment": {"project": project, "gid": gid}, "routes": routes}
    if resolvers:
        settings["resolvers"] = resolvers
    private_api = dict(api)
    for name in ("credential_file", "ca_file"):
        if name in private_api:
            private_api[name] = "/srv/nmrnet/input/" + Path(private_api[name]).name
    private_root = Path(directory) / "secrets"
    if not private_root.is_dir():
        private_root = Path(directory) / "configuration"
    api_path = private_root / "api.toml"
    with api_path.open("x") as stream:
        stream.write(tomli_w.dumps(private_api))
    api_path.chmod(0o600)
    if shared_fixture:
        origin = urlsplit(api["origin"])
        authority = f"{origin.hostname}:{origin.port if origin.port is not None else 443}"
        proxy_uid = 65534 if os.geteuid() != 65534 else 65533
        shared = {
            "deployment": {"project": project, "uid": os.geteuid(), "proxy_uid": proxy_uid,
                           "gid": gid, "request_seconds": 30},
            "api_proxy": {"max_connections": 32, "log_level": "INFO",
                          "allowed_authorities": [authority], "route": api_route},
            "outbox": {"provider_items": 8, "provider_bytes": 16777216,
                       "total_items": 8, "total_bytes": 16777216,
                       "retry_initial_seconds": 0.01, "retry_max_seconds": 0.1, "log_level": "INFO"},
        }
        shared_root = Path(directory) / "shared-settings"
        shared_root.mkdir(mode=0o750)
        shared_root.chmod(0o750)
        shared_path = shared_root / "settings.toml"
        with shared_path.open("x") as stream:
            stream.write(tomli_w.dumps(shared))
        shared_path.chmod(0o640)
    for name, value in (("provider", document), ("deployment", settings)):
        path = Path(directory) / (name + ".toml")
        with path.open("x") as stream:
            stream.write(tomli_w.dumps(value))
        path.chmod(0o600)
