"""Prepare immutable coordinate requests without loading model resources."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
from dataclasses import dataclass, field

from nmrnet_inference.predict import MAX_INPUT_BYTES, CoordinateUnitMismatch, parse_request


class InputRejected(ValueError):
    """Expected source/admission failure that the interpreter may explain or correct."""


@dataclass(frozen=True, slots=True)
class PreparedGeometry:
    """Keep exact executable bytes and their source mapping without mutable aliases.

    These are structurally admitted requests, not dictionary/model qualification.
    Payload bytes are excluded from repr because scientific input is private.
    """

    request_bytes: bytes = field(repr=False)
    source_sha256: str
    source_positions: tuple[int, ...]
    source_position_kind: str
    source_unit: str
    scale_to_angstrom: float


def _admit_request(raw):
    """Translate only the existing pure parser's expected rejection, not model errors."""
    try:
        request = parse_request(raw)
    except CoordinateUnitMismatch as error:
        raise InputRejected(
            f"{error} Direct JSON requires angstrom coordinates; use explicit preparation to convert other units."
        ) from None
    except ValueError as error:
        raise InputRejected(str(error)) from None
    if request["coordinate_unit"] != "angstrom":
        raise InputRejected(
            "Cannot prepare direct coordinates: coordinate_unit must be exactly angstrom. "
            "Use the explicit preparation route to convert other units."
        )
    return request


def prepare_json(source):
    """Admit declared coordinate JSON unchanged; malformed JSON never becomes prose."""
    request = _admit_request(source)
    return PreparedGeometry(
        request_bytes=source,
        source_sha256=hashlib.sha256(source).hexdigest(),
        source_positions=tuple(range(len(request["atoms"]))),
        source_position_kind="atom_index",
        source_unit="angstrom",
        scale_to_angstrom=1.0,
    )


def _table_rows(source):
    """Parse one contiguous element/X/Y/Z table, retaining original line numbers."""
    if type(source) is not bytes:
        raise TypeError("Coordinate table source must be immutable bytes.")
    if len(source) > MAX_INPUT_BYTES:
        raise InputRejected("Cannot prepare coordinate table: source exceeds the 1 MiB byte limit.")
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputRejected(f"Cannot prepare coordinate table: invalid UTF-8 at byte {error.start}.") from None
    rows = []
    ended = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            if rows:
                ended = True
            continue
        if ended:
            raise InputRejected(
                "Cannot prepare coordinate table: provide one geometry as contiguous atom rows; "
                "use an Upload for multiple geometries."
            )
        parts = line.split()
        if len(parts) != 4:
            raise InputRejected(f"Cannot prepare coordinate table at line {line_number}: expected element x y z.")
        try:
            coordinates = tuple(float(value) for value in parts[1:])
        except ValueError:
            raise InputRejected(f"Cannot prepare coordinate table at line {line_number}: x, y and z must be numbers.") from None
        rows.append((line_number, parts[0], coordinates))
    return rows


def prepare_table(source, *, source_unit):
    """Convert explicitly declared units once, then reuse scientific admission.

    The caller selects a bounded original source span, not model-retyped numbers.
    This initial reader accepts only contiguous element/X/Y/Z rows. Units are
    never guessed from magnitudes or bond lengths.
    """
    # NIST SP 811 Appendix B.8 lists angstrom -> nm = 0.1. Its inverse
    # nm -> angstrom is exactly ten, not a fit to plausible bond lengths.
    # https://www.nist.gov/pml/owm/metric-si-prefixes (consulted 2026-10-18)
    # https://www.nist.gov/pml/special-publication-811/nist-guide-si-appendix-b-conversion-factors/nist-guide-si-appendix-b8
    factors = {"angstrom": 1.0, "nm": 10.0}
    if type(source_unit) is not str or source_unit not in factors:
        raise InputRejected("Cannot prepare coordinate table: specify source unit angstrom or nm.")
    factor = factors[source_unit]
    rows = _table_rows(source)
    request = {
        "coordinate_unit": "angstrom",
        "atoms": [element for _, element, _ in rows],
        "coordinates": [[value * factor for value in coordinates] for _, _, coordinates in rows],
    }
    # Nonfinite values are rejected by the same parser as direct JSON; no second
    # floating-point admission policy or model resource load belongs here.
    raw = json.dumps(request, separators=(",", ":")).encode("utf-8")
    _admit_request(raw)
    return PreparedGeometry(
        request_bytes=raw,
        source_sha256=hashlib.sha256(source).hexdigest(),
        source_positions=tuple(line_number for line_number, _, _ in rows),
        source_position_kind="line_number",
        source_unit=source_unit,
        scale_to_angstrom=factor,
    )
