"""Record finite engine calls in a serialized, complete event directory."""

from tools.container_guard import require_container

require_container()

import fcntl
import json
import os


def record_engine_call(log, observed):
    # One file per call respects the client's inherited file-size cap. The
    # lock serializes admission while independent engine calls can still run.
    descriptor = os.open(log, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        paths = sorted(log.iterdir())
        for index, path in enumerate(paths):
            if path.name != f"{index:06d}.json":
                raise ValueError("Engine call evidence is not a complete ordered event directory.")
        event = log / f"{len(paths):06d}.json"
        pending = event.with_suffix(".pending")
        with pending.open("x") as stream:
            json.dump(observed, stream)
            stream.write("\n")
        pending.replace(event)
    finally:
        os.close(descriptor)


def read_engine_calls(log):
    # Missing or malformed events stay failed proof; never reconstruct history.
    descriptor = os.open(log, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        observed = []
        for index, path in enumerate(sorted(log.iterdir())):
            if path.name != f"{index:06d}.json":
                raise ValueError("Engine call evidence is not a complete ordered event directory.")
            observed.append(json.loads(path.read_text()))
        return observed
    finally:
        os.close(descriptor)
