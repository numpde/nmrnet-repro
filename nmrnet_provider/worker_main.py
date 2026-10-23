"""Compose the shared persistent supervisor with NMRNet's fixed scientific child.

The deployment supplies admitted role budgets and endpoint grants. Core owns
control, lease, process and scratch lifetimes; native_child owns scientific
startup and wire semantics. This entrypoint performs no model loading or API I/O.
"""

from tools.container_guard import require_container

require_container()

import argparse
import os
import sys

from provider_core.runner import main as supervise
from nmrnet_provider.worker_protocol import require_seconds


def prediction_lifetime_seconds(config):
    """Cover the admitted allocation-to-result path, without widening science calls.

    The phase counts come from runtime.assemble_attempt, controller._run_effects/
    _start_attempt and AttemptExecutor.run/_prepare_session/_monitor/_write_entry
    (021 T2 review).
    InputPreparation bounds selection and chooser retirement separately. No awaited
    condition is emitted by NMRNetSession; terminal publication follows child exit.
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


def main(arguments=None):
    parser = argparse.ArgumentParser(description="Run one admitted persistent native role.")
    parser.add_argument("role", choices=("prediction", "reader", "smiles"))
    for name in ("socket", "scratch"):
        parser.add_argument("--" + name, required=True)
    for name in ("io", "operation", "startup", "terminate", "kill", "cleanup"):
        parser.add_argument("--" + name + "-seconds", type=float, required=True)
    for name in ("seed", "max-iterations", "embedding-seconds"):
        parser.add_argument("--" + name, type=int)
    options = parser.parse_args(arguments)
    for name in ("io", "operation", "startup", "terminate", "kill", "cleanup"):
        require_seconds(getattr(options, name + "_seconds"))
    child = [sys.executable, "-I", "-B", "-m", "nmrnet_provider.native_child", options.role,
             "--io-seconds", str(options.io_seconds)]
    generation = options.seed, options.max_iterations, options.embedding_seconds
    if options.role == "smiles":
        if any(value is None for value in generation):
            parser.error("SMILES runner requires explicit seed, max-iterations and embedding-seconds.")
        from nmrnet_provider.smiles import GenerationSettings  # noqa: PLC0415 -- Only generation owns these settings.
        from nmrnet_provider.smiles_protocol import require_generation_deadline  # noqa: PLC0415 -- Admit before spawning.

        settings = GenerationSettings(*generation)
        require_generation_deadline(settings, options.io_seconds)
        for name, value in zip(("seed", "max-iterations", "embedding-seconds"), generation, strict=True):
            child.extend(("--" + name, str(value)))
    elif any(value is not None for value in generation):
        parser.error("Generation settings belong only to the SMILES runner.")
    supervisor = ["--role", options.role, "--socket", options.socket, "--scratch", options.scratch]
    for name in ("operation", "startup", "terminate", "kill", "cleanup"):
        supervisor.extend(("--" + name + "-seconds", str(getattr(options, name + "_seconds"))))
    supervise([*supervisor, "--", *child])


if __name__ == "__main__":
    from nmrnet_provider.runtime_logging import configure_logging

    configure_logging(os.environ.get("NMRNET_LOG_LEVEL", "INFO"))
    main()
