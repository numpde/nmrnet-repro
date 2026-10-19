"""Prepare immutable coordinate requests without loading model resources."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
from dataclasses import dataclass, field

from nmrnet_inference.predict import MAX_ATOMS, MAX_INPUT_BYTES, CoordinateUnitMismatch, parse_request


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
    conversion_reference: str
    source_record_number: int


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
        conversion_reference="identity (angstrom)",
        source_record_number=1,
    )


def decode_source(source):
    """Decode a bounded immutable coordinate source without echoing private bytes."""
    if type(source) is not bytes:
        raise TypeError("Coordinate source must be immutable bytes.")
    if len(source) > MAX_INPUT_BYTES:
        raise InputRejected("Cannot prepare coordinates: source exceeds the 1 MiB byte limit.")
    try:
        return source.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InputRejected(f"Cannot prepare coordinates: invalid UTF-8 at byte {error.start}.") from None


def parse_atom_row(line, line_number):
    """Parse the declared element/X/Y/Z columns and retain their source location."""
    parts = line.split()
    if len(parts) != 4:
        raise InputRejected(f"Cannot prepare coordinates at line {line_number}: expected element x y z.")
    try:
        coordinates = tuple(float(value) for value in parts[1:])
    except ValueError:
        raise InputRejected(f"Cannot prepare coordinates at line {line_number}: x, y and z must be numbers.") from None
    return line_number, parts[0], coordinates


def _table_rows(source):
    """Parse one contiguous element/X/Y/Z table, retaining original line numbers."""
    text = decode_source(source)
    rows = []
    ended = False
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            if rows:
                ended = True
            continue
        if ended:
            raise InputRejected(
                "Cannot read coordinate table: provide one geometry as contiguous atom rows, without blank lines between atoms."
            )
        if len(rows) >= MAX_ATOMS:
            raise InputRejected(f"Cannot read coordinate table: source exceeds the {MAX_ATOMS}-atom limit.")
        rows.append(parse_atom_row(line, line_number))
    return rows


def prepare_table(source, *, source_unit):
    """Convert declared units once, then validate the coordinate request structure.

    The caller selects a bounded original source span, not model-retyped numbers.
    This initial reader accepts only contiguous element/X/Y/Z rows. Units are
    never guessed from magnitudes or bond lengths.
    Dictionary membership and model-input checks happen later in PredictionSession.
    """
    return prepare_rows(_table_rows(source), source=source, source_unit=source_unit, source_record_number=1)


def coordinate_conversion(source_unit):
    """Return the declared conversion factor and its scientific reference."""
    # NIST SP 811 Appendix B.8 lists angstrom -> nm = 0.1. Its inverse
    # nm -> angstrom is exactly ten, not a fit to plausible bond lengths.
    # https://www.nist.gov/pml/owm/metric-si-prefixes (consulted 2026-10-18)
    # https://www.nist.gov/pml/special-publication-811/nist-guide-si-appendix-b-conversion-factors/nist-guide-si-appendix-b8
    # CODATA 2022, table XXXII: a0 = 5.29177210544(82) x 10^-11 m.
    # The bohr factor is the recommended central value, not an exact definition;
    # standard uncertainty is 8.2e-11 angstrom per bohr. Preserve the reference.
    # https://physics.nist.gov/cuu/pdf/JPCRD2022CODATA.pdf
    conversions = {
        "angstrom": (1.0, "identity (angstrom)"),
        "nm": (10.0, "NIST SP 811 Appendix B.8"),
        "bohr": (0.529177210544, "CODATA 2022"),
    }
    if type(source_unit) is not str or source_unit not in conversions:
        raise InputRejected("Cannot prepare coordinates: specify source unit angstrom, nm or bohr.")
    return conversions[source_unit]


def prepare_rows(rows, *, source, source_unit, source_record_number):
    """Convert parsed source rows once and freeze a structurally admitted request.

    Reader-owned rows and source positions are serialized immediately; this is an
    internal builder, not a second untrusted wire-format validator.
    """
    factor, reference = coordinate_conversion(source_unit)
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
        conversion_reference=reference,
        source_record_number=source_record_number,
    )
