"""Synthetic process events and raw fatal evidence; no science or API calls."""

from tools.container_guard import require_container

require_container()

import logging
import os
import signal
import sys
from pathlib import Path

from nmrnet_provider.runtime_logging import configure_logging


def main():
    mode = sys.argv[1]
    if mode not in ("success", "failure", "interrupted"):
        raise ValueError("Logging qualification requires its fixed process scenario.")
    configure_logging(os.environ["NMRNET_LOG_LEVEL"])
    logger = logging.getLogger("nmrnet_provider.logging_probe")
    logger.info("routine-start")
    logger.debug("owned-debug")
    dependency = logging.getLogger("qualification_dependency")
    dependency.setLevel(logging.DEBUG)
    dependency.info("DEPENDENCY_INFO_MUST_NOT_ESCAPE")
    dependency.warning("dependency-warning")
    # Synthetic scientific evidence goes directly through stderr into Docker
    # logs, including fatal exception notes. No model/API endpoint is used.
    sys.stderr.write("PRIVATE_NATIVE_DIAGNOSTIC\n")
    logger.warning("routine-warning")
    Path("/proof/ready").write_bytes(b"ready\n")
    if mode == "failure":
        failure = RuntimeError("PRIVATE_NATIVE_FAILURE")
        failure.add_note('completed_outcomes=[{"ppm":1.25,"source":"PRIVATE_MOLECULE"}]')
        raise failure
    if mode == "interrupted":
        signal.pause()
        raise AssertionError("Interrupted fixture unexpectedly resumed without termination.")
    logger.info("routine-complete")


if __name__ == "__main__":
    main()
