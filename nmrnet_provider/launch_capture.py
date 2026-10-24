"""Capture the named deployment's declared execution inputs.

The host supplies already inspected image/revision identities and a read-only
source root and explicit file names. Declared provenance remains caller-owned;
hashes describe the actual source bytes read here. The helper owns no store, network or worker.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json

from nmrnet_provider.config import ConfigurationRejected
from nmrnet_provider.process import _declared_execution, _publish, _source_name
from nmrnet_provider.startup_capture import _bind_path, _source_path


def capture_execution(source_root, destination, source_names, **identities):
    """Hash the explicitly granted source files into required execution configuration."""
    source_root = _bind_path(source_root)
    names = tuple(source_names)
    if not names or len(set(names)) != len(names):
        raise ConfigurationRejected("execution source names must be explicit and distinct.")
    sources = {}
    for name in names:
        _source_name(name)
        path = _source_path(source_root / name, source_root)
        # These are individual read-only source grants, not credential files.
        # Ordinary checkout modes remain valid; capture the bytes actually read.
        with path.open("rb") as source:
            raw = source.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ConfigurationRejected("execution source exceeds its 1048576-byte ceiling.")
        sources[name] = {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    execution = _declared_execution(dict(identities, runtime_identity_source="caller", tool_sources=sources))
    encoded = (json.dumps(execution, sort_keys=True) + "\n").encode()
    if len(encoded) > 65536:
        raise ConfigurationRejected("host execution declaration exceeds its 65536-byte admission ceiling.")
    _publish(destination, "execution.json", encoded)
