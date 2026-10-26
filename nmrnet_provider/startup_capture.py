"""Admit startup paths, destination authorities and the prediction lifetime.

Deployment and finite recovery reuse these narrow rules. They do not construct
runtime owners or allocate native work; each caller owns its captured files.
"""

from tools.container_guard import require_container

require_container()

import os
from pathlib import Path
from urllib.parse import urlsplit

from nmrnet_provider.config import ConfigurationRejected
from provider_core.provider_proxy import ServiceRefused, authority_parts


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

    Acquisition follows START and completed input selection/chooser retirement.
    Counts cover the remaining AttemptExecutor preparation-monitor observation,
    running progress, durable entry, active observations and prediction exchanges.
    Terminal publication follows child exit; this binding emits no awaited condition.
    """
    lifecycle, native = config.lifecycle, config.service.native
    exchange = config.api.http.exchange_seconds + config.api.http.retirement_seconds
    gap = lifecycle.observation_gap_seconds
    # Include the original and a late admitted running-progress exchange.
    phase_progress = 2 * (exchange + config.api.http.retirement_seconds + gap)
    # Preparation-monitor overhangs, durable-entry observation and eligibility
    # reads around prediction each have the existing active-freshness gap cap.
    observations = 5 * gap
    durable_entry = lifecycle.store_seconds
    science = 2 * native.prediction_seconds
    return sum((native.allocation_seconds, phase_progress, observations, durable_entry, science))


def startup_authorities(config):
    """Admit the fixed CONNECT destinations without projecting configuration secrets."""
    return {
        "api": [_authority(config.api.identity.origin)],
        "interpreter": sorted({_authority(endpoint.spec.url) for endpoint in config.interpreter.endpoints}),
        "store": [_authority(config.uploads.store_origin)],
    }


def _private_file(path, flags):
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)
