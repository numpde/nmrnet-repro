"""Routine process logs; scientific diagnostics keep their private channels.

Configure only at executable entrypoints. Stdout is a normal StreamHandler,
not a replacement stream: RDKit's scoped stderr capture and raw fatal exception
notes remain private. Docker owns collection and host journald owns retention.
"""

from tools.container_guard import require_container

require_container()

import logging
import sys


def _routine_record(record):
    # Dependencies may set their own logger levels. The sink must enforce their
    # WARNING floor too; the root logger's level does not filter propagation.
    owned = any(record.name == name or record.name.startswith(name + ".") for name in ("nmrnet_provider", "nmrnet_inference"))
    return owned or record.levelno >= logging.WARNING


def configure_logging(level):
    """Keep dependency defaults at WARNING, with explicit owned INFO or DEBUG."""
    if level not in ("INFO", "DEBUG", "WARNING"):
        raise ValueError("Cannot configure routine logging: choose INFO, DEBUG or WARNING.")
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_routine_record)
    logging.basicConfig(
        level=logging.WARNING,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    for name in ("nmrnet_provider", "nmrnet_inference"):
        logging.getLogger(name).setLevel(level)
