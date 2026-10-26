"""Author isolated qualification inputs for the normal provider and shared owners."""

import os
from pathlib import Path

import tomli_w


def write_deployment_inputs(directory, document, *, api, api_route, routes, project,
                            maximum_startup_bytes, resolvers):
    """Keep API identity in one neutral definition and science in its provider input."""
    document.pop("api")
    document["delivery"].pop("socket_path")
    document["delivery"].pop("socket_group_id")
    settings = {"maximum_startup_bytes": maximum_startup_bytes, "routes": routes}
    if resolvers:
        settings["resolvers"] = resolvers
    bindings = {
        "project": project, "uid": os.geteuid(), "gid": os.getegid(),
        "request_seconds": 30, "retry_initial_seconds": 0.01, "retry_max_seconds": 0.1,
        "limits": {"provider_items": 8, "provider_bytes": 16777216,
                   "total_items": 8, "total_bytes": 16777216},
        "bindings": [{"name": "nmrnet", "socket_path": "/run/delivery/http.sock",
                      "api": api, "route": api_route}],
    }
    for name, value in (("provider", document), ("deployment", settings), ("bindings", bindings)):
        path = Path(directory) / (name + ".toml")
        with path.open("x") as stream:
            stream.write(tomli_w.dumps(value))
        path.chmod(0o600)
