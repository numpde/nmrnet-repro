"""Prepare explicitly selected plain XYZ frames without inferring a bond graph."""

from tools.container_guard import require_container

require_container()

import re

from nmrnet_inference.predict import MAX_ATOMS

from .input_geometry import InputRejected, decode_source, parse_atom_row, prepare_rows

# This is an internal reader work bound, not an advertised provider batch limit.
# The offering must choose measured lower limits against its entire result budget.
MAX_RECORDS = 128


def _frames(source):
    """Locate plain XYZ frames without interpreting unselected atom coordinates.

    Plain XYZ is count, comment, then N element/X/Y/Z rows. Concatenation keeps
    frame identity; no bond graph or same-molecule claim follows from this syntax.
    https://openbabel.org/docs/FileFormats/XYZ_cartesian_coordinates_format.html
    Consulted 2026-10-18. Extended XYZ needs a separate schema-aware reader and is
    deliberately rejected rather than guessing columns from its Properties field.
    """
    lines = decode_source(source).splitlines()
    frames = []
    position = 0
    while position < len(lines):
        record = len(frames) + 1
        if record > MAX_RECORDS:
            raise InputRejected("Cannot prepare XYZ: source exceeds the 128-record limit.")
        if re.fullmatch(r"[1-9][0-9]*", lines[position].strip()) is None:
            raise InputRejected(f"Cannot locate XYZ record {record}: expected a positive atom count at line {position + 1}.")
        # Bound integer parsing and work before slicing/allocating atom records.
        if len(lines[position].strip()) > 6:
            raise InputRejected(f"Cannot locate XYZ record {record}: atom count exceeds the source work limit.")
        count = int(lines[position])
        end = position + 2 + count
        if end > len(lines):
            raise InputRejected(f"Cannot locate XYZ record {record}: comment or atom rows are missing.")
        frames.append((position + 2, end))
        position = end
    if not frames:
        raise InputRejected("Cannot prepare XYZ: source contains no geometry.")
    return lines, tuple(frames)


def prepare_xyz(source, *, source_unit, selection):
    """Prepare all frames or an explicit ordered tuple of one-based record numbers.

    Source units are a required caller decision; comments and coordinate magnitude
    cannot silently change them. Malformed selected records fail the entire call.
    Only structural frame boundaries must be readable in unselected records.
    """
    lines, frames = _frames(source)
    if selection == "all":
        records = tuple(range(1, len(frames) + 1))
    elif (
        type(selection) is tuple
        and selection
        and all(type(number) is int and 1 <= number <= len(frames) for number in selection)
        and len(set(selection)) == len(selection)
    ):
        records = selection
    else:
        raise InputRejected("Cannot select XYZ records: choose all or unique existing positive record numbers.")
    prepared = []
    for record in records:
        start, end = frames[record - 1]
        if end - start > MAX_ATOMS:
            raise InputRejected(f"Cannot prepare XYZ record {record}: atom count exceeds {MAX_ATOMS}.")
        if "Properties=" in lines[start - 1] or "Lattice=" in lines[start - 1]:
            raise InputRejected(f"Cannot prepare XYZ record {record}: extended XYZ metadata needs a supported explicit reader.")
        try:
            rows = [parse_atom_row(lines[index], index + 1) for index in range(start, end)]
            geometry = prepare_rows(
                rows, source=source, source_unit=source_unit, source_record_number=record, source_position_kind="line_number"
            )
        except InputRejected as error:
            raise InputRejected(f"Cannot prepare XYZ record {record}: {error}") from None
        prepared.append(geometry)
    return tuple(prepared)


def prepare_pasted_xyz(source, *, source_unit):
    """Read exactly one XYZ frame; no conformer relationship is inferred."""
    _, frames = _frames(source)
    if len(frames) != 1:
        raise InputRejected("Cannot read pasted XYZ: provide exactly one XYZ frame; this reader does not accept multiple frames.")
    return prepare_xyz(source, source_unit=source_unit, selection=(1,))[0]
