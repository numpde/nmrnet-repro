"""Standard process logging collected with raw stderr by Docker.

Configure only at executable entrypoints. Stdout is a normal StreamHandler,
not a replacement stream. RDKit's scoped diagnostic capture is unchanged; raw
fatal exception notes reach Docker logs. Deployment owns bounded retention.
"""

from tools.container_guard import require_container

require_container()

import logging
import sys

# These namespaces receive owned verbosity; other loggers retain the dependency WARNING floor.
# repo-check: allow-module-constant
_OWNED_LOGGERS = ("nmrnet_provider", "nmrnet_inference", "provider_core.runner")


def _routine_record(record):
    # Dependencies may set their own logger levels. The sink must enforce their
    # WARNING floor too; the root logger's level does not filter propagation.
    owned = any(record.name == name or record.name.startswith(name + ".") for name in _OWNED_LOGGERS)
    return owned or record.levelno >= logging.WARNING


def configure_logging(level):
    """Use owned INFO unless an explicit override selects INFO, DEBUG or WARNING."""
    if level is not None and level not in ("INFO", "DEBUG", "WARNING"):
        raise ValueError("Cannot configure routine logging: choose INFO, DEBUG or WARNING.")
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_routine_record)
    logging.basicConfig(
        level=logging.WARNING,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    for name in _OWNED_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        if level is not None:
            logger.setLevel(level)
