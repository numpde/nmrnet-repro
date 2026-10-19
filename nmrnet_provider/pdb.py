"""Extract supplied PDB coordinates without constructing or changing a bond graph.

Authority: wwPDB format 3.3, coordinate section MODEL, ATOM, HETATM and ENDMDL:
https://www.wwpdb.org/documentation/file-format-content/format33/sect9.html
END is the final coordinate-entry record:
https://www.wwpdb.org/documentation/file-format-content/format33/sect11.html#END
Consulted 2026-10-19. Fixed-column extraction keeps alternate selection and atom
correspondence under one coordinate reader, without native graph construction.
"""

from tools.container_guard import require_container

require_container()

import re

from nmrnet_inference.predict import MAX_ATOMS
from nmrnet_provider.input_geometry import InputRejected, coordinate_conversion, decode_source, prepare_rows
from nmrnet_provider.xyz import MAX_RECORDS


def _positive_id(field, subject):
    if re.fullmatch(r"[0-9]+", field.strip()) is None or int(field) < 1:
        raise InputRejected(f"Cannot read PDB {subject}: a positive decimal identifier is required.")
    return int(field)


def _models(text):
    """Frame all models before selecting chemistry; END never hides later rows."""
    models, implicit = {}, []
    active = None
    ended = False
    for number, original_line in enumerate(text.split("\n"), start=1):
        line = original_line.removesuffix("\r")
        if not line.strip():
            continue
        if ended:
            raise InputRejected(f"Cannot read PDB: nonblank content follows END at line {number}.")
        kind = line[:6]
        if kind == "MODEL ":
            if not line[:14].isascii() or line[6:10].strip(" ") or line[14:].strip(" "):
                raise InputRejected(
                    f"Cannot read PDB MODEL at line {number}: use only its ASCII identifier columns and blank padding."
                )
            if active is not None or implicit:
                raise InputRejected("Cannot read PDB: nested models or mixed implicit and explicit atom regions are unsupported.")
            active = _positive_id(line[10:14], f"MODEL at line {number}")
            if active in models:
                raise InputRejected("Cannot read PDB: MODEL identifiers must be unique.")
            if len(models) >= MAX_RECORDS:
                raise InputRejected("Cannot read PDB: source exceeds the 128-model limit.")
            models[active] = []
        elif kind == "ENDMDL":
            if line[6:].strip(" "):
                raise InputRejected(f"Cannot read PDB ENDMDL at line {number}: fields after the record name must be blank.")
            if active is None:
                raise InputRejected("Cannot read PDB: ENDMDL has no open MODEL.")
            active = None
        elif kind == "END   " or line.rstrip(" ") == "END":
            if line[3:].strip(" "):
                raise InputRejected(f"Cannot read PDB END at line {number}: fields after the record name must be blank.")
            if active is not None:
                raise InputRejected("Cannot read PDB: MODEL requires ENDMDL before END.")
            ended = True
        elif kind in {"ATOM  ", "HETATM"}:
            if models and active is None:
                raise InputRejected("Cannot read PDB: atom rows outside declared MODEL regions are unsupported.")
            target = models[active] if models else implicit
            target.append((number, line))
        elif line.split(maxsplit=1)[0] in {"ATOM", "HETATM", "MODEL", "ENDMDL", "END"}:
            raise InputRejected(f"Cannot read PDB: malformed coordinate record columns at line {number}.")
    if not ended:
        raise InputRejected("Cannot read PDB: the final END record is missing; provide a complete coordinate entry.")
    if not models:
        if not implicit:
            raise InputRejected("Cannot read PDB: source contains no atom coordinates.")
        # With no declared MODEL, 1 is the sole source-record ordinal, not a
        # fabricated deposited model serial. The caller selects it consistently.
        models[1] = implicit
    return models


def _atom_sites(records):
    """Identify alternate sites before parsing selected coordinate values.

    ATOM columns 13-27 identify atom/residue/chain; 7-11 identify source serials.
    Even an unselected alternate needs a unique serial in its selected model:
    retained source references use that serial, not an ambiguous row occurrence.
    Coordinate/element values of those unselected alternates remain opaque.
    A residue-name or ATOM/HETATM conflict cannot manufacture a second site.
    Blank plus nonblank labels at the same site is ambiguous, not a common atom.
    """
    sites, serials = {}, set()
    for number, line in records:
        if len(line) < 27 or not line[:27].isascii() or not line[:27].isprintable():
            raise InputRejected(f"Cannot identify PDB atom at line {number}: provide ASCII atom and residue columns.")
        serial = _positive_id(line[6:11], f"atom serial at line {number}")
        if serial in serials:
            raise InputRejected("Cannot read PDB: atom serials must be unique within each selected model.")
        serials.add(serial)
        if not line[12:16].strip() or re.fullmatch(r"-?[0-9]+", line[22:26].strip()) is None:
            raise InputRejected(f"Cannot read PDB atom at line {number}: atom name and decimal residue number are required.")
        site = (line[21], int(line[22:26]), line[26], line[12:16].strip())
        metadata = (line[:6], line[17:20])
        alternate = line[16].strip()
        if site not in sites:
            if len(sites) >= MAX_ATOMS:
                raise InputRejected("Cannot read PDB: selected model exceeds the 128-atom limit.")
            sites[site] = metadata, {}
        identity, alternatives = sites[site]
        if identity != metadata:
            raise InputRejected(
                "Cannot read PDB: conflicting residue or record names at one atom site require a separately selected structure."
            )
        if alternate in alternatives:
            raise InputRejected("Cannot read PDB: duplicate alternate-location labels at one atom site are ambiguous.")
        alternatives[alternate] = (number, serial, line)
    return sites


def _selected_rows(records, alternate_location):
    chosen = []
    for _, alternatives in _atom_sites(records).values():
        if "" in alternatives:
            if len(alternatives) != 1:
                raise InputRejected(
                    "Cannot read PDB: blank and nonblank alternate locations at the same atom site are ambiguous."
                )
            chosen.append(alternatives[""])
        else:
            if alternate_location is None:
                raise InputRejected(
                    "Cannot read PDB: choose an explicit alternate-location label; occupancy does not select one."
                )
            if alternate_location not in alternatives:
                raise InputRejected("Cannot read PDB: the chosen alternate-location label is missing at an atom site.")
            chosen.append(alternatives[alternate_location])
    # Grouping selects sites; original source-line order owns the output order.
    rows = []
    for number, serial, line in sorted(chosen):
        if len(line) < 78 or not line[:78].isascii() or not line[:78].isprintable():
            raise InputRejected(f"Cannot read PDB atom at line {number}: provide ASCII fixed columns through the element field.")
        element = line[76:78].strip()
        if re.fullmatch(r"[A-Za-z]{1,2}", element) is None or element.upper() in {"D", "T"}:
            raise InputRejected(
                f"Cannot read PDB atom at line {number}: explicit element columns are required; isotope labels D/T are unsupported."
            )
        try:
            coordinates = tuple(float(line[start : start + 8]) for start in (30, 38, 46))
        except ValueError:
            raise InputRejected(f"Cannot read PDB atom at line {number}: fixed X/Y/Z columns must contain numbers.") from None
        rows.append((serial, element.capitalize(), coordinates))
    return rows


def prepare_pdb(source, *, source_unit, selection, alternate_location):
    """Read all models or ordered unique source MODEL serials, without alignment.

    A file without MODEL has source-record ordinal 1. PDB 3.3 defines angstrom
    coordinates; source_unit is the caller's explicit assertion, permitting a
    documented override for nonstandard user-supplied files. Never infer units
    from magnitudes. The interpreter owns the evidence for that assertion.
    One uniform alternate label must resolve every nonblank site; independent
    per-residue choices are outside this supported subset. CONECT is unused.
    """
    coordinate_conversion(source_unit)
    models = _models(decode_source(source))
    if alternate_location is not None and (
        type(alternate_location) is not str or len(alternate_location) != 1 or not "!" <= alternate_location <= "~"
    ):
        raise InputRejected(
            "Cannot select PDB alternate locations: supply one printable nonblank ASCII label or leave the choice unspecified."
        )
    if type(selection) is str and selection == "all":
        selected = tuple(models)
    elif (
        type(selection) is tuple
        and selection
        and all(type(number) is int and number in models for number in selection)
        and len(set(selection)) == len(selection)
    ):
        selected = selection
    else:
        raise InputRejected("Cannot select PDB models: choose all or unique existing source model identifiers.")
    geometries = []
    for model in selected:
        try:
            rows = _selected_rows(models[model], alternate_location)
            geometry = prepare_rows(
                rows, source=source, source_unit=source_unit, source_record_number=model, source_position_kind="atom_serial"
            )
        except InputRejected as cause:
            raise InputRejected(f"Cannot read PDB model {model}: {cause}") from None
        geometries.append(geometry)
    return tuple(geometries)
