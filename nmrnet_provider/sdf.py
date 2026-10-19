"""Select supplied V2000 SDF structures while leaving associated data opaque.

MDL CTFile Formats (October 2003), chapter 6, printed pp. 39-41, defines the
record delimiter and blank-terminated data items. Original specification mirror:
https://www.daylight.com/meetings/mug05/Kappler/ctfile.pdf
Printed p. 40 defines a delimiter as a line beginning with four dollar signs,
so suffix text does not turn that line into an opaque data value.
These boundaries prevent silently treating an appended structure as properties.
The qualified MOL reader owns dimensions, atom identities and native parsing;
this module never checks molecular equivalence or generates missing atoms.
"""

from tools.container_guard import require_container

require_container()

import hashlib
from dataclasses import replace

from nmrnet_provider.input_geometry import InputRejected, coordinate_conversion, decode_source
from nmrnet_provider.mol import prepare_mol
from nmrnet_provider.xyz import MAX_RECORDS


def _records(text):
    """Frame the whole source before selected structures reach the native reader."""
    records = []
    current = []
    # Match the native MOL owner's LF/CRLF framing. Unicode separators are data,
    # not additional structure headers or record delimiters.
    for raw_line in text.split("\n"):
        line = raw_line.removesuffix("\r")
        if line.startswith("$$$$"):
            if not any(row.strip() for row in current):
                raise InputRejected(f"Cannot locate SDF record {len(records) + 1}: the record is empty.")
            if len(records) == MAX_RECORDS:
                raise InputRejected("Cannot prepare SDF: source exceeds the 128-record limit.")
            records.append(current)
            current = []
        else:
            current.append(line)
    if any(line.strip() for line in current):
        raise InputRejected(
            f"Cannot locate SDF record {len(records) + 1}: its closing $$$$ delimiter is missing; "
            "use the MOL reader for a single unframed structure."
        )
    if not records:
        raise InputRejected("Cannot prepare SDF: source contains no structure records.")
    return records


def _mol_block(lines, number):
    """Separate one selected MOL block from structurally framed, opaque data items."""
    try:
        end = lines.index("M  END")
    except ValueError:
        raise InputRejected(f"Cannot prepare SDF record {number}: its MOL structure has no M  END terminator.") from None
    in_data = False
    for line in lines[end + 1 :]:
        if not line.strip():
            in_data = False
        elif not in_data:
            if not line.startswith(">") or not line[1:].strip():
                raise InputRejected(
                    f"Cannot prepare SDF record {number}: content after M  END must be a data item "
                    "starting with >; another structure requires its own $$$$ delimiter."
                )
            in_data = True
    if in_data:
        raise InputRejected(
            f"Cannot prepare SDF record {number}: the last data item needs a blank line before its $$$$ delimiter."
        )
    return ("\n".join(lines[: end + 1]) + "\n").encode("utf-8")


def prepare_sdf(source, *, source_unit, selection):
    """Return all records or an ordered unique tuple of one-based selections.

    Whole-source UTF-8, byte bounds and record framing must be valid even for
    unselected records. Only selected MOL blocks and data-item framing are read;
    unselected invalid chemistry or blocked V3000 never enters a native parser.
    A failed selection returns no partial tuple. Source identity always names the
    original complete SDF, including unselected records and opaque data values.
    """
    text = decode_source(source)
    coordinate_conversion(source_unit)
    records = _records(text)
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
        raise InputRejected("Cannot select SDF records: choose all or unique existing positive record numbers, up to 128.")
    source_sha256 = hashlib.sha256(source).hexdigest()
    prepared = []
    for number in selected:
        block = _mol_block(records[number - 1], number)
        try:
            geometry = prepare_mol(block, source_unit=source_unit)
        except InputRejected as cause:
            # The public receiver renders only str(); the native diagnostic and
            # original rejection remain separately available to protected owners.
            raise InputRejected(f"Cannot prepare SDF record {number}: {cause}", diagnostic=cause.diagnostic) from cause
        except Exception as cause:
            # MOL's inconclusive native failures stay operational. This owner adds
            # only the selected record identity, without replacing type or cause.
            cause.add_note(f"While preparing SDF record {number} from the selected source.")
            raise
        prepared.append(replace(geometry, source_sha256=source_sha256, source_record_number=number))
    return tuple(prepared)
