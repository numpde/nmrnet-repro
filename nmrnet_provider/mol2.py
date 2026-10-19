"""Extract supplied MOL2 atom coordinates without interpreting a molecular graph.

Tripos Mol2 File Format, SYBYL 7.1 (Mid-2005), printed/PDF p. 3 gives section
framing; p. 12 gives ATOM IDs/XYZ/types; pp. 53-54 give concrete SYBYL types:
https://zhanggroup.org/DockRMSD/mol2.pdf#page=3
https://zhanggroup.org/DockRMSD/mol2.pdf#page=12
https://zhanggroup.org/DockRMSD/mol2.pdf#page=53
Verified 2026-10-19. The original vendor manual is hosted on a mirror.
Graph, charge, alternate-type and attribute sections are unused. This projection
does not validate molecular identity, stereochemistry, isotope state or bonding.
"""

from tools.container_guard import require_container

require_container()

import re

from nmrnet_inference.predict import MAX_ATOMS
from nmrnet_provider.input_geometry import InputRejected, coordinate_conversion, decode_source, prepare_rows
from nmrnet_provider.xyz import MAX_RECORDS

# Source IDs reach JSON result consumers. Keep them exact even in a binary64
# reader; this is our transport bound, not a claimed Tripos integer-width rule.
MAX_ATOM_ID = 2**53 - 1

# float() also accepts Python digit separators. File coordinates use decimal
# numbers with an optional exponent; they must not silently reinterpret 1_0.
_COORDINATE_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")

# These are the concrete dotted types in the cited pp. 53-54 table. Prefix
# guessing would turn Du.C into carbon or accept an unknown producer type.
_SYBYL_SUFFIXES = {
    "C": {"3", "2", "1", "ar", "cat"},
    "N": {"3", "2", "1", "ar", "am", "pl3", "4"},
    "O": {"3", "2", "co2", "spc", "t3p"},
    "S": {"3", "2", "O", "O2"},
    "P": {"3"},
    "H": {"spc", "t3p"},
    "Cr": {"th", "oh"},
    "Co": {"oh"},
}


def _positive_number(token, maximum, subject):
    # Strip padding before bounding conversion; large decimal source tokens
    # cannot trigger Python's integer digit limit or create huge output IDs.
    digits = token.lstrip("0")
    if re.fullmatch(r"[0-9]+", token) is None or not digits or len(digits) > len(str(maximum)) or int(digits) > maximum:
        raise InputRejected(f"Cannot read MOL2: {subject} must be a decimal integer from 1 through {maximum}.")
    return int(digits)


def _records(text):
    """Frame the entire bounded source, leaving unselected record contents opaque."""
    records = []
    for raw in text.split("\n"):
        line = raw.removesuffix("\r")
        if line.lstrip(" \t").startswith("@<TRIPOS>") and line[:1] != "@":
            raise InputRejected("Cannot frame MOL2: section markers must start with @ in column 1.")
        marker = line.rstrip(" \t")
        if marker == "@<TRIPOS>MOLECULE":
            if len(records) >= MAX_RECORDS:
                raise InputRejected("Cannot read MOL2: source exceeds the 128-record limit.")
            records.append([])
        elif marker.startswith("@<TRIPOS>MOLECULE"):
            raise InputRejected("Cannot frame MOL2: MOLECULE must occupy its own section-marker line.")
        elif records:
            records[-1].append(line)
        elif marker and not marker.startswith("#"):
            raise InputRejected("Cannot frame MOL2: content before the first MOLECULE must be blank or a comment.")
    if not records:
        raise InputRejected("Cannot read MOL2: source contains no MOLECULE records.")
    return records


def _content(lines):
    return [line for line in lines if line.strip() and not line.lstrip(" \t").startswith("#")]


def _element(atom_type, row_number):
    """Project the finite SYBYL vocabulary, without treating atom names as elements.

    Further bare element spellings are a declared reader extension; the existing
    request parser and later model dictionary still own usable element admission.
    Pseudo/query types and D/T labels cannot become supplied ordinary nuclei.
    """
    if atom_type in {"LP", "Du", "Du.C", "Any", "Hal", "Het", "Hev", "D", "T"}:
        raise InputRejected(
            f"Cannot read MOL2 ATOM row {row_number}: pseudo/query and isotope labels require an explicitly supported concrete element."
        )
    # Two observed aliases, not general case folding: Open Babel 3.1.1's
    # ReadMolecule normalizes these; MDAnalysis 2.9.0 SYBYL2SYMB lists both cases.
    # https://github.com/openbabel/openbabel/blob/openbabel-3-1-1/src/formats/mol2format.cpp#L269-L272
    # https://docs.mdanalysis.org/2.9.0/_modules/MDAnalysis/guesser/tables.html
    if atom_type in {"S.o", "S.o2"}:
        return "S"
    if "." not in atom_type:
        if re.fullmatch(r"[A-Z][a-z]?", atom_type) is not None:
            return atom_type
    else:
        symbol, suffix = atom_type.split(".", 1)
        if symbol in _SYBYL_SUFFIXES and suffix in _SYBYL_SUFFIXES[symbol]:
            return symbol
    raise InputRejected(
        f"Cannot read MOL2 ATOM row {row_number}: use a documented concrete SYBYL type or a bare element symbol; custom type sets are unsupported."
    )


def _atom_rows(lines):
    sections = [(index, line.rstrip(" \t")) for index, line in enumerate(lines) if line.startswith("@<TRIPOS>")]
    if any(marker.startswith("@<TRIPOS>ATOM") and marker != "@<TRIPOS>ATOM" for _, marker in sections):
        raise InputRejected("Cannot read MOL2: ATOM must occupy its own section-marker line.")
    atom_sections = [index for index, marker in sections if marker == "@<TRIPOS>ATOM"]
    if len(atom_sections) != 1:
        raise InputRejected("Cannot read MOL2: each selected record requires exactly one ATOM section.")
    header = _content(lines[: sections[0][0]])
    if len(header) < 2:
        raise InputRejected("Cannot read MOL2: the MOLECULE name and atom-count line are required.")
    count = _positive_number(header[1].split(maxsplit=1)[0], MAX_ATOMS, "selected atom count")
    start = atom_sections[0]
    end = next((index for index, _ in sections if index > start), len(lines))
    entries = _content(lines[start + 1 : end])
    if len(entries) != count:
        raise InputRejected("Cannot read MOL2: ATOM row count differs from the declared atom count.")
    rows, identifiers = [], set()
    for number, line in enumerate(entries, start=1):
        fields = line.split()
        if len(fields) < 6 or line.rstrip().endswith("\\"):
            raise InputRejected(f"Cannot read MOL2 ATOM row {number}: provide ID, name, X/Y/Z and type on one physical line.")
        identifier = _positive_number(fields[0], MAX_ATOM_ID, f"source atom ID at ATOM row {number}")
        if identifier in identifiers:
            raise InputRejected(
                f"Cannot read MOL2 ATOM row {number}: source atom IDs must be unique within each selected record."
            )
        identifiers.add(identifier)
        symbol = _element(fields[5], number)
        if not all(_COORDINATE_NUMBER.fullmatch(value) is not None for value in fields[2:5]):
            raise InputRejected(f"Cannot read MOL2 ATOM row {number}: X/Y/Z must be decimal numbers with optional exponents.")
        coordinates = tuple(float(value) for value in fields[2:5])
        rows.append((identifier, symbol, coordinates))
    return rows


def prepare_mol2(source, *, source_unit, selection):
    """Read all records or an ordered unique tuple of one-based source ordinals.

    Supplied atoms and coordinates survive unchanged apart from the declared unit
    conversion. Bonds/charges/attributes do not add, remove or reorder atoms. All
    selected records succeed before any geometry is returned; no native parser,
    molecular-equivalence check or geometry generation participates.
    """
    records = _records(decode_source(source))
    coordinate_conversion(source_unit)
    if type(selection) is str and selection == "all":
        selected = tuple(range(1, len(records) + 1))
    elif (
        type(selection) is tuple
        and 1 <= len(selection) <= MAX_RECORDS
        and all(type(number) is int and 1 <= number <= len(records) for number in selection)
        and len(set(selection)) == len(selection)
    ):
        selected = selection
    else:
        raise InputRejected("Cannot select MOL2 records: choose all or unique existing positive record ordinals, up to 128.")
    geometries = []
    for record in selected:
        try:
            geometry = prepare_rows(
                _atom_rows(records[record - 1]),
                source=source,
                source_unit=source_unit,
                source_record_number=record,
                source_position_kind="atom_id",
            )
        except InputRejected as cause:
            raise InputRejected(f"Cannot read MOL2 record {record}: {cause}", diagnostic=cause.diagnostic) from cause
        geometries.append(geometry)
    return tuple(geometries)
