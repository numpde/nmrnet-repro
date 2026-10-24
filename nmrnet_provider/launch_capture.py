"""Capture the named deployment's declared execution inputs.

The host supplies already inspected image/revision identities and a read-only
native capture. Their declared provenance remains caller-owned; hashes describe
the frozen bytes actually read here. The helper owns no store, network or worker.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json

from nmrnet_provider.config import ConfigurationRejected, read_startup_file
from nmrnet_provider.process import _declared_execution, _publish, _source_name
from nmrnet_provider.startup_capture import _bind_path


def capture_execution(native, destination, **identities):
    """Join the capture inventory to its bytes before publishing a declaration."""
    native = _bind_path(native)
    revision = read_startup_file(native / "revision", maximum_bytes=41, label="frozen native revision")
    if revision != (identities["repository_revision"] + "\n").encode():
        raise ConfigurationRejected("frozen native revision differs from the declared execution revision.")
    inventory = read_startup_file(native / "inputs.paths", maximum_bytes=65536, label="frozen native inventory")
    try:
        names = inventory.decode("ascii").splitlines()
    except UnicodeError as cause:
        raise ConfigurationRejected("frozen native inventory must contain canonical file names.", cause=cause) from None
    if not names or names != sorted(set(names)) or inventory != ("\n".join(names) + "\n").encode():
        raise ConfigurationRejected("frozen native inventory must contain distinct sorted complete file names.")
    sources, rows = {}, []
    for name in names:
        _source_name(name)
        path = _bind_path(native / "sources" / name)
        # Source files include the 100780-byte inference lock at 92493f0.
        # Their 1 MiB read ceiling is independent of the 64 KiB JSON declaration.
        raw = read_startup_file(path, maximum_bytes=1024 * 1024, label="frozen native source")
        digest = hashlib.sha256(raw).hexdigest()
        sources[name] = {"size": len(raw), "sha256": digest}
        rows.append(digest + "  " + name + "\n")
    manifest = read_startup_file(native / "sources.sha256", maximum_bytes=65536, label="frozen native source hashes")
    # sha256sum preserves the caller's capture order; compare the complete rows
    # without treating that order as a different source identity.
    if sorted(manifest.splitlines(keepends=True)) != sorted(row.encode() for row in rows):
        raise ConfigurationRejected("frozen native bytes or inventory differ from their retained source hashes.")
    execution = _declared_execution(dict(identities, runtime_identity_source="caller", tool_sources=sources))
    encoded = (json.dumps(execution, sort_keys=True) + "\n").encode()
    if len(encoded) > 65536:
        raise ConfigurationRejected("host execution declaration exceeds its 65536-byte admission ceiling.")
    _publish(destination, "execution.json", encoded)
