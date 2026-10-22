"""Freeze a stopped recovery's exact TOML and explicitly granted API files.

The metadata pass reads only the original TOML. Inspect never reads signing or
trust files; run reuses the configuration owner's API-only admission. Neither
pass opens Attempt storage, creates a transport or acquires scientific resources.
"""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from nmrnet_provider.config import (
    ConfigurationRejected,
    _read_recovery_config,
    _read_recovery_input_paths,
    _read_recovery_inspection,
    read_startup_file,
)
from nmrnet_provider.process import _publish
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.startup_capture import _authority, _bind_path, _private_file, _source_path

_FIELDS = ("source_root", "config_relative_path", "config_sha256", "store_root", "authority", "credential_file", "ca_file")


def _metadata(path, *, mode, source_root, maximum_bytes):
    if mode not in ("inspect", "run"):
        raise ConfigurationRejected("recovery capture requires the explicit inspect or run operation.")
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise ConfigurationRejected("recovery capture requires an explicit positive aggregate byte ceiling.")
    source_root = _bind_path(source_root)
    if not source_root.is_dir():
        raise ConfigurationRejected("recovery capture requires the original canonical startup source directory.")
    path = _source_path(path, source_root)
    raw = read_startup_file(path, maximum_bytes=65536, label="configuration")
    if len(raw) > maximum_bytes:
        raise ConfigurationRejected("recovery capture exceeds its aggregate byte ceiling.")

    def document(value, **_settings):
        if _source_path(value, source_root) != path:
            raise ConfigurationRejected("recovery metadata may read only its original TOML.")
        return raw

    inspection = _read_recovery_inspection(path, document)
    store_root = _bind_path(inspection.lifecycle.store_root)
    if store_root.is_relative_to(source_root) or source_root.is_relative_to(store_root):
        raise ConfigurationRejected("the Attempt store must remain separate from recovery's read-only startup inputs.")
    fields = [str(source_root), str(path.relative_to(source_root)), hashlib.sha256(raw).hexdigest(), str(store_root), "", "", ""]
    if mode == "run":
        paths = tuple(_source_path(value, source_root) for value in _read_recovery_input_paths(path, document))
        if len(paths) not in (2, 3) or paths[0] != path:
            raise ConfigurationRejected("API recovery requires a separate explicit signing artifact and optional CA file.")
        fields[4] = _authority(inspection.lifecycle.scope.api_origin)
        fields[5] = str(paths[1])
        if len(paths) == 3:
            fields[6] = str(paths[2])
    return tuple(fields), path, raw


def recovery_metadata(path, *, mode, source_root, maximum_bytes):
    """Return seven fixed secret-free host fields after reading only TOML."""
    fields, _path, _raw = _metadata(path, mode=mode, source_root=source_root, maximum_bytes=maximum_bytes)
    return fields


def capture_recovery(path, *, mode, source_root, destination, expected_config_sha256, maximum_bytes):
    """Publish a frozen plan only after exact metadata and all owned reads agree."""
    if type(expected_config_sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", expected_config_sha256) is None:
        raise ConfigurationRejected("recovery capture requires the exact original metadata configuration SHA-256.")
    fields, path, original = _metadata(path, mode=mode, source_root=source_root, maximum_bytes=maximum_bytes)
    if fields[2] != expected_config_sha256:
        raise ConfigurationRejected("the original recovery TOML changed after metadata admission.")
    source_root, destination = _bind_path(source_root), _bind_path(destination)
    store_root = Path(fields[3])
    if destination.is_relative_to(source_root) or source_root.is_relative_to(destination):
        raise ConfigurationRejected("recovery capture requires a fresh destination outside the original source grant.")
    if destination.is_relative_to(store_root) or store_root.is_relative_to(destination):
        raise ConfigurationRejected("recovery capture must remain separate from the existing Attempt store.")
    expected = {path, *(Path(value) for value in fields[5:] if value)}
    destination.mkdir(mode=0o700)
    inputs = destination / "inputs"
    inputs.mkdir(mode=0o700)
    captured, total = {}, 0

    def retain(value, **settings):
        nonlocal total
        source = _source_path(value, source_root)
        if source not in expected:
            raise ConfigurationRejected("recovery admission requested a file outside its exact metadata grants.")
        raw = original if source == path else read_startup_file(source, **settings)
        if source in captured:
            if captured[source] != raw:
                raise ConfigurationRejected("a repeated recovery input changed during capture.")
            return raw
        if total + len(raw) > maximum_bytes:
            raise ConfigurationRejected("recovery capture exceeds its aggregate byte ceiling; retain its private partial output.")
        relative = source.relative_to(source_root)
        parent = inputs
        for part in relative.parts[:-1]:
            parent /= part
            if not parent.is_dir():
                parent.mkdir(mode=0o700)
        with open(parent / relative.name, "xb", opener=_private_file) as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
        captured[source] = raw
        total += len(raw)
        return raw

    if mode == "inspect":
        _read_recovery_inspection(path, retain)
    else:
        _read_recovery_config(path, retain)
    if captured.keys() != expected:
        raise ConfigurationRejected("recovery admission did not consume exactly its metadata file grants.")
    _publish(destination, "plan.json", (json.dumps(dict(zip(_FIELDS, fields, strict=True)), sort_keys=True) + "\n").encode())
    return fields


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Capture only stopped recovery inputs; never open storage or network.")
    commands = parser.add_subparsers(dest="operation", required=True)
    for operation in ("metadata", "capture"):
        command = commands.add_parser(operation)
        command.add_argument("--mode", choices=("inspect", "run"), required=True)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--source-root", type=Path, required=True)
        command.add_argument("--maximum-bytes", type=int, required=True)
        command.add_argument("--handoff", type=Path, required=True)
        if operation == "capture":
            command.add_argument("--destination", type=Path, required=True)
            command.add_argument("--expected-config-sha256", required=True)
    options = parser.parse_args(arguments)
    try:
        arguments = {"mode": options.mode, "source_root": options.source_root, "maximum_bytes": options.maximum_bytes}
        if options.operation == "metadata":
            fields = recovery_metadata(options.config, **arguments)
        else:
            fields = capture_recovery(
                options.config,
                destination=options.destination,
                expected_config_sha256=options.expected_config_sha256,
                **arguments,
            )
        _publish(options.handoff.parent, options.handoff.name, b"".join(value.encode("utf-8") + b"\0" for value in fields))
    except (OSError, ConfigurationRejected) as cause:
        sys.stderr.write(json.dumps(failure_evidence(cause, secrets=())) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
