"""Private scientific frames shared by the contained controller and worker.

Four-byte network-order lengths and a 2 MiB cap follow SECS
34f5915c1534278bbf61b4c6592afa44cf94ab08,
src/secs_inference/provider/worker.py::_send/_receive/_exact. These messages carry
private input/results, never API credentials or container-retirement authority.
"""

from tools.container_guard import require_container

require_container()

import json
import math
import struct

MAX_FRAME_BYTES = 2 * 1024 * 1024


class WorkerProtocolError(ValueError):
    """The scientific exchange cannot continue; worker retirement remains separate."""


def require_seconds(seconds):
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("Worker operation duration must be finite and positive")


def require_allocation(allocation):
    if (
        type(allocation) is not str
        or not 1 <= len(allocation) <= 128
        or not allocation.isascii()
        or not all(char.isalnum() or char in "-_" for char in allocation)
    ):
        raise ValueError("Worker allocation requires 1 through 128 ASCII letters, digits, hyphens or underscores")


def frame(document):
    raw = json.dumps(document, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    if not 1 <= len(raw) <= MAX_FRAME_BYTES:
        raise WorkerProtocolError("Scientific frame exceeds the 2 MiB limit; no partial frame was sent")
    return struct.pack("!I", len(raw)) + raw


def frame_size(header):
    size = struct.unpack("!I", header)[0]
    if not 1 <= size <= MAX_FRAME_BYTES:
        raise WorkerProtocolError("Scientific frame length must be between 1 byte and 2 MiB")
    return size


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkerProtocolError("Scientific frame contains a repeated field")
        result[key] = value
    return result


def _constant(_value):
    raise WorkerProtocolError("Scientific frame contains a nonfinite JSON constant")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise WorkerProtocolError("Scientific frame contains an out-of-range number")
    return number


def document(raw):
    try:
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_constant, parse_float=_finite_float)
    except (ValueError, UnicodeError, RecursionError) as cause:
        raise WorkerProtocolError("Cannot decode scientific frame as strict JSON") from cause
    if type(value) is not dict:
        raise WorkerProtocolError("Scientific frame must contain one JSON object")
    return value
