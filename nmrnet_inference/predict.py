"""Admit one bounded coordinate request before scientific imports.

The input identity and parsed request come from the same bytes. Coordinate-unit
meaning and dictionary membership remain separate admission decisions.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import math
import re
import struct

MAX_INPUT_BYTES = 1 << 20
MAX_ATOMS = 128
REQUEST_FIELDS = ("coordinate_unit", "atoms", "coordinates")


def read_request_bytes(source):
    """Read at most the byte limit plus one and identify the accepted bytes."""
    content = bytearray()
    while len(content) <= MAX_INPUT_BYTES:
        remaining = MAX_INPUT_BYTES + 1 - len(content)
        if remaining == 0:
            break
        chunk = source.read(remaining)
        if chunk is None:
            raise BlockingIOError("Cannot read prediction input: the source is not ready.")
        if chunk == b"":
            break
        content.extend(chunk)

    raw = bytes(content)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Cannot read prediction input: request exceeds the 1 MiB byte limit.")
    identity = {"size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    return raw, identity


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Cannot parse prediction input: a JSON object contains a duplicate key.")
        result[key] = value
    return result


def _reject_json_constant(_constant):
    raise ValueError("Cannot parse prediction input: JSON numbers must be finite.")


def _parse_json_integer(digits):
    try:
        return int(digits)
    except ValueError:
        raise ValueError(
            "Cannot parse prediction input: integer literal exceeds the supported digit limit."
        ) from None


def _float32_coordinate(value, atom_index, axis_index):
    field = f"coordinates[{atom_index}][{axis_index}]"
    if type(value) not in (int, float):
        raise ValueError(f"Cannot parse prediction input: {field} must be a finite number, not a boolean or string.")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError(f"Cannot parse prediction input: {field} is outside the finite numeric range.") from None
    if not math.isfinite(number):
        raise ValueError(f"Cannot parse prediction input: {field} must be finite.")
    try:
        converted = struct.unpack("!f", struct.pack("!f", number))[0]
    except OverflowError:
        raise ValueError(f"Cannot parse prediction input: {field} overflows float32.") from None
    if not math.isfinite(converted):
        raise ValueError(f"Cannot parse prediction input: {field} is not finite as float32.")
    return converted


def parse_request(raw):
    """Validate request structure and return coordinates rounded to float32.

    This checks only the coordinate-unit field's string shape. External unit
    admission waits for the release-coordinate provenance gate to close.
    """
    if type(raw) is not bytes:
        raise TypeError("Cannot parse prediction input: request bytes must be bytes.")
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Cannot parse prediction input: request exceeds the 1 MiB byte limit.")
    parse_error = None
    try:
        request = json.loads(
            raw,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
            parse_int=_parse_json_integer,
        )
    except json.JSONDecodeError as error:
        parse_error = (
            f"Cannot parse prediction input: JSON syntax is invalid at line {error.lineno}, "
            f"column {error.colno}: {error.msg}."
        )
    except UnicodeDecodeError as error:
        parse_error = (
            "Cannot parse prediction input: request is not UTF-8 encoded JSON; "
            f"invalid byte at position {error.start}."
        )
    except RecursionError:
        raise ValueError("Cannot parse prediction input: JSON nesting exceeds the supported depth.") from None

    if parse_error is not None:
        # Do not chain the decoder error: its object retains the caller's full
        # request bytes even though the safe diagnostic needs only location data.
        raise ValueError(parse_error)

    if type(request) is not dict:
        raise ValueError("Cannot parse prediction input: the top-level JSON value must be an object.")
    for field in REQUEST_FIELDS:
        if field not in request:
            raise ValueError(f"Cannot parse prediction input: required field {field!r} is missing.")
    if set(request) != set(REQUEST_FIELDS):
        raise ValueError("Cannot parse prediction input: only coordinate_unit, atoms, and coordinates are allowed.")

    coordinate_unit = request["coordinate_unit"]
    if type(coordinate_unit) is not str or not coordinate_unit.strip():
        raise ValueError("Cannot parse prediction input: coordinate_unit must be a non-empty string.")

    atoms = request["atoms"]
    if type(atoms) is not list or not 1 <= len(atoms) <= MAX_ATOMS:
        raise ValueError(f"Cannot parse prediction input: atoms must contain 1 through {MAX_ATOMS} symbols.")
    for index, atom in enumerate(atoms):
        if type(atom) is not str or re.fullmatch(r"[A-Z][a-z]?", atom) is None:
            raise ValueError(f"Cannot parse prediction input: atoms[{index}] must be an element-symbol spelling.")

    coordinates = request["coordinates"]
    if type(coordinates) is not list or len(coordinates) != len(atoms):
        raise ValueError("Cannot parse prediction input: coordinates must contain one row per atom.")
    float32_coordinates = []
    for atom_index, row in enumerate(coordinates):
        if type(row) is not list or len(row) != 3:
            raise ValueError(f"Cannot parse prediction input: coordinates[{atom_index}] must contain three values.")
        float32_coordinates.append(
            [_float32_coordinate(value, atom_index, axis_index) for axis_index, value in enumerate(row)]
        )

    return {
        "coordinate_unit": coordinate_unit,
        "atoms": atoms,
        "coordinates": float32_coordinates,
    }
