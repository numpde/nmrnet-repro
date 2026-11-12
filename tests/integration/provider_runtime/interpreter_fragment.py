"""Read and identify the private interpreter fragment."""

from tools.container_guard import require_container

require_container()

import hashlib
import tomllib

from nmrnet_provider.config import read_startup_file
from nmrnet_provider.startup_capture import _bind_path


def interpreter_fragment(path):
    """Read one private fragment and identify the exact files needed for admission.

    This first pass receives no key grant. The existing interpreter owner admits
    endpoint semantics after the host grants the referenced individual files.
    """
    raw = read_startup_file(path, maximum_bytes=65536, label="interpreter configuration", secret=True)
    document = tomllib.loads(raw.decode("utf-8"))
    endpoint = document
    if not {"url", "model", "key_file"} <= endpoint.keys() or endpoint.keys() - {
        "url",
        "model",
        "key_file",
        "ca_file",
        "reasoning_effort",
    }:
        raise ValueError("Cannot stage interpreter configuration: provide one flat endpoint with documented fields")
    inputs = {
        "configuration_sha256": hashlib.sha256(raw).hexdigest(),
        "key_file": str(_bind_path(endpoint["key_file"])),
        # silent-defaults: allow-contract -- An omitted CA has no file to stage; a supplied CA path passes bind-path validation before staging copies it. StageApiConfigTests covers both selections.
        "ca_file": str(_bind_path(endpoint["ca_file"])) if "ca_file" in endpoint else None,
    }
    return endpoint, inputs
