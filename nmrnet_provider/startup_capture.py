"""Freeze only the explicit configuration bytes consumed by the startup owner.

The offline host helper grants one read-only source root and a fresh private
destination. Runtime mounts destination/inputs at the original absolute source
root: TOML paths and source_sha256 remain unchanged. System CA roots belong to
the same immutable controller image; execution/model declarations are separate
host grants. No network, Attempt store or worker is acquired here.
"""

from tools.container_guard import require_container

require_container()

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

from nmrnet_provider.config import ConfigurationRejected, _read_provider_config, read_startup_file
from nmrnet_provider.process import _publish
from nmrnet_provider.process_reports import failure_evidence
from tools.provider_proxy import ServiceRefused, authority_parts


def _bind_path(value):
    if not isinstance(value, (str, Path)):
        raise ConfigurationRejected("startup bind paths require explicit absolute paths.")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or any(character in str(path) for character in (",", "\n", "\r", "\x00")):
        raise ConfigurationRejected("startup bind paths require canonical absolute paths without symlinks or mount delimiters.")
    try:
        resolved = path.resolve(strict=False)
    except RuntimeError as cause:
        raise ConfigurationRejected("startup bind paths must resolve without a symlink loop.", cause=cause) from None
    if resolved != path:
        raise ConfigurationRejected("startup bind paths require canonical absolute paths without symlinks or mount delimiters.")
    return path


def _source_path(value, root):
    path = _bind_path(value)
    if not path.is_relative_to(root):
        raise ConfigurationRejected("startup files must remain inside the explicit source root.")
    return path


def _authority(url):
    parts = urlsplit(url)
    # silent-defaults: allow-contract -- HTTPS uses 443 when no port is specified; an explicit port must be preserved and pass the proxy's admission below.
    port = 443 if parts.port is None else parts.port
    authority = f"{parts.hostname}:{port}"
    # The existing CONNECT listener owns supported host/port grammar. Refuse
    # an unusable grant rather than changing the configured destination.
    try:
        authority_parts(authority)
    except ServiceRefused as cause:
        raise ConfigurationRejected(str(cause), cause=cause) from None
    return authority


def prediction_lifetime_seconds(config):
    """Cover allocation-to-result while preserving the finer client deadlines.

    Counts follow runtime.assemble_attempt, controller._run_effects/_start_attempt
    and AttemptExecutor.run/_prepare_session/_monitor/_write_entry (021 T2 review).
    Selection and chooser retirement have separate preparation budgets. Terminal
    publication follows child exit; this binding emits no awaited condition.
    """
    lifecycle, native = config.lifecycle, config.service.native
    exchange = config.api.http.exchange_seconds + config.api.http.retirement_seconds
    gap = lifecycle.observation_gap_seconds
    inventories = 3 * lifecycle.store_seconds
    start = 3 * lifecycle.store_seconds + 2 * exchange + lifecycle.start_retry_window_seconds + config.api.http.retirement_seconds
    input_read = exchange
    phase_progress = 2 * (exchange + config.api.http.retirement_seconds + gap)
    selection_and_chooser_cleanup = 2 * config.preparation.seconds
    # Initial eligibility, two preparation-monitor overhangs, durable-entry
    # observation and explicit eligibility before prediction each have a gap cap.
    observations = 5 * gap
    durable_entry = lifecycle.store_seconds
    science = 2 * native.prediction_seconds
    return sum((native.allocation_seconds, inventories, start, input_read, phase_progress,
                selection_and_chooser_cleanup, observations, durable_entry, science))


def _host_plan(config, source_root, config_path):
    """Project named host consumers only; configuration objects contain secrets."""
    native, generation = config.service.native, config.preparation.generation_settings
    return {
        "source_root": str(source_root),
        "config_relative_path": str(config_path.relative_to(source_root)),
        "config_sha256": config.source_sha256,
        "authorities": {
            "api": [_authority(config.api.identity.origin)],
            "interpreter": sorted({_authority(endpoint.spec.url) for endpoint in config.interpreter.endpoints}),
            "store": [_authority(config.uploads.store_origin)],
        },
        "store_root": str(config.lifecycle.store_root),
        "native": {
            "allocation_seconds": native.allocation_seconds,
            "prediction_seconds": native.prediction_seconds,
            "source_seconds": native.source_seconds,
            "generation_seconds": config.preparation.generation_seconds,
            "embedding_seconds": generation.timeout_seconds,
            "seed": generation.seed,
            "max_iterations": generation.max_iterations,
            "max_worker_calls": config.preparation.max_worker_calls,
        },
        "shutdown": {
            "drain_seconds": config.service.policy.drain_seconds,
            "settle_seconds": config.service.policy.settle_seconds,
        },
    }


def capture_startup(path, *, source_root, destination, maximum_bytes):
    """Retain admitted bytes once; publish a ready plan only after every close.

    Partial captures remain private evidence. Repeated file references repeat
    their owner's admission, including secret mode and per-read size limits;
    changed bytes refuse rather than choosing one meaning for the same path.
    The host must keep this source tree immutable while capture runs and grant
    only the frozen inputs to the subsequent runtime.
    """
    source_root, destination = _bind_path(source_root), _bind_path(destination)
    if not source_root.is_dir():
        raise ConfigurationRejected("startup source root must be an explicit canonical directory without symlinks.")
    if destination.is_relative_to(source_root) or source_root.is_relative_to(destination):
        raise ConfigurationRejected("startup capture requires a fresh absolute destination outside its source grant.")
    if type(maximum_bytes) is not int or maximum_bytes < 1:
        raise ConfigurationRejected("startup capture requires an explicit positive aggregate byte ceiling.")
    config_path = _source_path(path, source_root)
    destination.mkdir(mode=0o700)
    inputs = destination / "inputs"
    inputs.mkdir(mode=0o700)
    captured, total = {}, 0

    def retain(value, **settings):
        nonlocal total
        source = _source_path(value, source_root)
        raw = read_startup_file(source, **settings)
        if source in captured:
            if captured[source] != raw:
                raise ConfigurationRejected("a repeated startup file changed during capture; no runtime plan was admitted.")
            return raw
        if total + len(raw) > maximum_bytes:
            raise ConfigurationRejected("startup capture exceeds its aggregate byte ceiling; no runtime plan was admitted.")
        relative = source.relative_to(source_root)
        parent = inputs
        # mkdir(parents=True) would apply its mode only to the last directory;
        # each receiving subtree must be private even under a permissive mask.
        for part in relative.parts[:-1]:
            parent /= part
            if not parent.is_dir():
                parent.mkdir(mode=0o700)
        target = parent / relative.name
        with open(target, "xb", opener=_private_file) as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
        captured[source], total = raw, total + len(raw)
        return raw

    config = _read_provider_config(config_path, retain)
    store = _bind_path(config.lifecycle.store_root)
    if store.is_relative_to(source_root) or source_root.is_relative_to(store):
        raise ConfigurationRejected("the writable Attempt store must remain outside the read-only startup source grant.")
    if store.is_relative_to(destination) or destination.is_relative_to(store):
        raise ConfigurationRejected("the writable Attempt store must remain separate from the frozen startup capture.")
    plan = _host_plan(config, source_root, config_path)
    _publish(destination, "plan.json", (json.dumps(plan, sort_keys=True) + "\n").encode())
    return plan


def _private_file(path, flags):
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Capture admitted startup files offline without changing their TOML paths.")
    for name in ("config", "source-root", "destination"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--maximum-bytes", type=int, required=True)
    options = parser.parse_args(arguments)
    try:
        capture_startup(
            options.config, source_root=options.source_root, destination=options.destination, maximum_bytes=options.maximum_bytes
        )
    except (OSError, ConfigurationRejected) as cause:
        # Docker collects stderr. No arbitrary exception text
        # or configuration representation is emitted on this offline boundary.
        sys.stderr.write(json.dumps(failure_evidence(cause, secrets=())) + "\n")
        raise SystemExit(2) from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
