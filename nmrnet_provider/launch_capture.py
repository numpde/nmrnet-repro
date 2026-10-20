"""Produce the foreground caller's private startup handoff without runtime effects.

The host supplies already inspected image/revision identities and a read-only
native capture. Their declared provenance remains caller-owned; hashes describe
the frozen bytes actually read here. The helper owns no store, network or worker.
"""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import json
import sys
from pathlib import Path

from nmrnet_provider.config import ConfigurationRejected, read_startup_file
from nmrnet_provider.process import _declared_execution, _publish, _source_name
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.startup_capture import _bind_path, capture_startup


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


def host_fields(plan):
    """Emit the 17 fixed Bash inputs; no shell syntax or secret fields cross."""
    native, shutdown, authorities = plan["native"], plan["shutdown"], plan["authorities"]
    fields = [plan["source_root"], plan["config_relative_path"], plan["config_sha256"]]
    fields.extend("\n".join(authorities[role]) for role in ("api", "interpreter", "store"))
    fields.append(plan["store_root"])
    fields.extend(
        native[name]
        for name in (
            "allocation_seconds",
            "prediction_seconds",
            "source_seconds",
            "generation_seconds",
            "embedding_seconds",
            "seed",
            "max_iterations",
            "max_worker_calls",
        )
    )
    fields.extend(shutdown[name] for name in ("drain_seconds", "settle_seconds"))
    return b"".join(str(value).encode() + b"\0" for value in fields)


def main(arguments=None):
    parser = argparse.ArgumentParser(
        description="Capture the foreground service's admitted startup bytes and declared execution facts."
    )
    for name in ("config", "source-root", "destination", "native-capture"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--maximum-bytes", type=int, required=True)
    identity_names = ("image-id", "geometry-image-id", "controller-image-id", "repository-revision", "upstream-revision")
    for name in identity_names:
        parser.add_argument("--" + name, required=True)
    options = parser.parse_args(arguments)
    try:
        plan = capture_startup(
            options.config, source_root=options.source_root, destination=options.destination, maximum_bytes=options.maximum_bytes
        )
        identities = {name.replace("-", "_"): getattr(options, name.replace("-", "_")) for name in identity_names}
        capture_execution(options.native_capture, options.destination, **identities)
        sys.stdout.buffer.write(host_fields(plan))
    except (OSError, ConfigurationRejected) as cause:
        sys.stderr.write(json.dumps(failure_evidence(cause, secrets=())) + "\n")
        raise SystemExit(2) from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
