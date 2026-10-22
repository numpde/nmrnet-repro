"""Carry source bytes into the reader and frozen geometry back, without model I/O.

The frame and lifecycle owners are shared with prediction. Source identity and
unit provenance come from the caller's exact bytes/options, not worker-supplied
copies. Prepared bytes, source locations and protected reader diagnostics return
over the wire. The existing whole-frame limit includes those diagnostics.
MOL/SDF and pure PDB/MOL2 reading stay in the isolated reader; controller admission
never invokes RDKit or reparses their sources. Other native formats and conformer
generation require separate qualification.
"""

from tools.container_guard import require_container

require_container()

import base64
import binascii
import hashlib
import json

from nmrnet_inference.predict import MAX_INPUT_BYTES
from nmrnet_provider.input_geometry import (
    InputRejected,
    PreparedGeometry,
    coordinate_conversion,
    prepare_json,
    prepare_table,
)
from nmrnet_provider.mol import prepare_mol
from nmrnet_provider.mol2 import MAX_ATOM_ID, prepare_mol2
from nmrnet_provider.pdb import prepare_pdb
from nmrnet_provider.sdf import prepare_sdf
from nmrnet_provider.upload_archive import MAX_LISTING_BYTES, MAX_MEMBERS, inspect_upload, is_archive, select_member
from nmrnet_provider.worker_protocol import WorkerProtocolError
from nmrnet_provider.xyz import MAX_RECORDS, prepare_xyz


def diagnostic_text(value):
    """Admit optional private text without clipping the reader's captured evidence."""
    if value is not None and type(value) is not str:
        raise WorkerProtocolError("Structure worker diagnostic must be text or null.")
    return value


def require_read_options(source_format, source_unit, selection, alternate_location=None):
    """Reject reading options before a caller acquires source or worker resources."""
    if source_format not in ("json", "table", "xyz", "mol", "sdf", "pdb", "mol2"):
        raise InputRejected("Cannot read structure: choose json, table, xyz, mol, sdf, pdb or mol2.")
    if source_format == "pdb":
        if alternate_location is not None and (
            type(alternate_location) is not str or len(alternate_location) != 1 or not "!" <= alternate_location <= "~"
        ):
            raise InputRejected("Cannot select PDB alternate locations: supply one printable nonblank ASCII label.")
    elif alternate_location is not None:
        raise InputRejected("Cannot select alternate locations: this option applies only to PDB sources.")
    coordinate_conversion(source_unit)
    if source_format == "json" and source_unit != "angstrom":
        raise InputRejected("Cannot read direct coordinate JSON: its source unit must be angstrom.")
    if source_format not in ("xyz", "sdf", "pdb", "mol2"):
        if selection is not None:
            raise InputRejected("Cannot select records: JSON, coordinate tables and MOL contain one geometry; omit selection.")
    elif selection != "all" and not (
        type(selection) is tuple
        and 1 <= len(selection) <= MAX_RECORDS
        and all(type(number) is int and 1 <= number <= (9999 if source_format == "pdb" else MAX_RECORDS) for number in selection)
        and len(set(selection)) == len(selection)
    ):
        raise InputRejected(
            "Cannot select structure records: choose all or at most 128 unique positive source identifiers within the format's range."
        )


def decode_source_bytes(encoded):
    if type(encoded) is not str or len(encoded) > 4 * ((MAX_INPUT_BYTES + 2) // 3):
        raise WorkerProtocolError("Cannot read structure request: encoded source exceeds the 1 MiB source limit or is not text.")
    try:
        source = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as cause:
        raise WorkerProtocolError("Cannot read structure request: source is not valid base64.") from cause
    if len(source) > MAX_INPUT_BYTES:
        raise WorkerProtocolError("Cannot read structure request: decoded source exceeds the 1 MiB source limit.")
    return source


def source_request(source, *, source_format, source_unit, selection, alternate_location=None):
    """Admit explicit reading options and the source-byte bound before dispatch."""
    if type(source) is not bytes:
        raise TypeError("Cannot read structure: source must be immutable bytes.")
    if len(source) > MAX_INPUT_BYTES:
        raise InputRejected("Cannot read structure: source exceeds the 1 MiB byte limit.")
    require_read_options(source_format, source_unit, selection, alternate_location)
    request = {
        "operation": "read_geometry",
        "source_base64": base64.b64encode(source).decode("ascii"),
        "source_format": source_format,
        "source_unit": source_unit,
        "selection": list(selection) if type(selection) is tuple else selection,
    }
    if source_format == "pdb":
        request["alternate_location"] = alternate_location
    return request


def upload_inspection_request(source):
    """Bound an acquired immutable snapshot before allocating its wire frame."""
    if type(source) is not bytes:
        raise TypeError("Cannot inspect Upload: source must be immutable bytes.")
    if len(source) > MAX_INPUT_BYTES:
        raise InputRejected("Cannot inspect Upload: source exceeds the 1 MiB byte limit.")
    return {"operation": "inspect_upload", "source_base64": base64.b64encode(source).decode("ascii")}


def inspect_upload_request(request):
    if set(request) != {"operation", "source_base64"} or request["operation"] != "inspect_upload":
        raise WorkerProtocolError("Upload inspection accepts only its exact operation and source bytes.")
    return inspect_upload(decode_source_bytes(request["source_base64"]))


def admit_upload_inventory(inventory):
    """Admit metadata only; declared sizes never establish decoded byte identity."""
    if type(inventory) is not dict or set(inventory) != {"container", "members"}:
        raise WorkerProtocolError("Upload inspection returned incomplete source metadata.")
    members = inventory["members"]
    if inventory["container"] not in ("direct", "zip") or type(members) is not list or len(members) > MAX_MEMBERS:
        raise WorkerProtocolError("Upload inspection returned an unsupported container or excessive member count.")
    if inventory["container"] == "direct" and members:
        raise WorkerProtocolError("Direct Upload inspection cannot declare archive members.")
    for member in members:
        if (
            type(member) is not dict
            or set(member) != {"name", "byte_length"}
            or type(member["name"]) is not str
            or type(member["byte_length"]) is not int
            or member["byte_length"] < 0
        ):
            raise WorkerProtocolError("Upload inspection returned malformed member metadata.")
    if len(json.dumps(inventory, ensure_ascii=True).encode("ascii")) > MAX_LISTING_BYTES:
        raise WorkerProtocolError("Upload inspection returned an excessive member listing.")
    return inventory


def read_member_source(request):
    """Decode exactly one logical member, then use the existing format reader."""
    if request.get("operation") != "read_upload_member" or type(request.get("member")) is not str:
        raise WorkerProtocolError("ZIP preparation requires its exact operation and a member name.")
    reading = request.copy()
    del reading["member"]
    reading["operation"] = "read_geometry"
    options = _read_options(reading)
    selected = select_member(decode_source_bytes(request["source_base64"]), request["member"])
    records = _read_records(selected, *options)
    return selected, records


def read_source(request):
    """Invoke one existing reader; the complete selected batch precedes any reply."""
    options = _read_options(request)
    return _read_records(decode_source_bytes(request["source_base64"]), *options)


def _read_options(request):
    """Admit the closed request and options before decoding any archive member."""
    fields = {"operation", "source_base64", "source_format", "source_unit", "selection"}
    if request.get("source_format") == "pdb":
        fields.add("alternate_location")
    if set(request) != fields or request["operation"] != "read_geometry":
        raise WorkerProtocolError("Structure worker accepts only read_geometry with its exact source and option fields.")
    selection = tuple(request["selection"]) if type(request["selection"]) is list else request["selection"]
    source_format, source_unit = request["source_format"], request["source_unit"]
    alternate_location = request["alternate_location"] if source_format == "pdb" else None
    require_read_options(source_format, source_unit, selection, alternate_location)
    return source_format, source_unit, selection, alternate_location


def _read_records(source, source_format, source_unit, selection, alternate_location):
    if is_archive(source):
        raise InputRejected("Cannot read a ZIP as molecular coordinates: call inspect_upload and select an exact member name.")
    if source_format == "json":
        geometries = (prepare_json(source),)
    elif source_format == "table":
        geometries = (prepare_table(source, source_unit=source_unit),)
    elif source_format == "xyz":
        geometries = prepare_xyz(source, source_unit=source_unit, selection=selection)
    elif source_format == "mol":
        geometries = (prepare_mol(source, source_unit=source_unit),)
    elif source_format == "pdb":
        geometries = prepare_pdb(source, source_unit=source_unit, selection=selection, alternate_location=alternate_location)
    elif source_format == "mol2":
        geometries = prepare_mol2(source, source_unit=source_unit, selection=selection)
    else:
        geometries = prepare_sdf(source, source_unit=source_unit, selection=selection)
    return [
        {
            "request_base64": base64.b64encode(geometry.request_bytes).decode("ascii"),
            "source_positions": geometry.source_positions,
            "source_position_kind": geometry.source_position_kind,
            "source_record_number": geometry.source_record_number,
            "diagnostic": diagnostic_text(geometry.diagnostic),
        }
        for geometry in geometries
    ]


def prepared_reply(reply, source, *, source_format, source_unit, selection):
    """Validate the cross-process result before reconstructing immutable geometry.

    The qualified reader owns parsing and atom correspondence. This boundary
    rejects malformed locations, wrong explicit selections, changed direct JSON and invalid
    executable requests; it does not reparse native source files in the controller.
    Reader fixture qualification establishes scientific correspondence; wire
    validation alone cannot establish that a parser interpreted the source correctly.
    The reader also owns the complete source population for "all"; this receiver
    checks contiguous returned record IDs, or unique PDB source-record identifiers
    (declared MODEL serials, otherwise the sole block's ordinal 1).
    Explicit selections have a caller-known
    record count and order to check independently.
    """
    if set(reply) != {"kind", "allocation", "records"} or reply["kind"] != "geometries":
        raise WorkerProtocolError("Structure worker returned no complete geometry collection.")
    records = reply["records"]
    if type(records) is not list or not 1 <= len(records) <= MAX_RECORDS:
        raise WorkerProtocolError("Structure worker must return between 1 and 128 geometry records.")
    expected_numbers = tuple(range(1, len(records) + 1)) if selection == "all" else selection
    if source_format == "pdb" and selection == "all":
        expected_numbers = tuple(record.get("source_record_number") if type(record) is dict else None for record in records)
        if any(type(number) is not int or not 1 <= number <= 9999 for number in expected_numbers) or len(
            set(expected_numbers)
        ) != len(records):
            raise WorkerProtocolError(
                "Structure worker must return unique positive PDB source-record identifiers in their source order."
            )
    elif source_format not in ("xyz", "sdf", "pdb", "mol2"):
        expected_numbers = (1,)
    if len(records) != len(expected_numbers):
        raise WorkerProtocolError("Structure worker record count differs from the requested selection.")
    source_sha256 = hashlib.sha256(source).hexdigest()
    factor, reference = coordinate_conversion(source_unit)
    line_count = len(source.splitlines())
    prepared = []
    for record, number in zip(records, expected_numbers, strict=True):
        geometry = _prepared_record(record, number, source, source_format, line_count)
        prepared.append(
            PreparedGeometry(
                request_bytes=geometry.request_bytes,
                source_sha256=source_sha256,
                source_positions=tuple(record["source_positions"]),
                source_position_kind=record["source_position_kind"],
                source_unit=source_unit,
                scale_to_angstrom=factor,
                conversion_reference=reference,
                source_record_number=number,
                diagnostic=diagnostic_text(record["diagnostic"]),
            )
        )
    return tuple(prepared)


def _prepared_record(record, number, source, source_format, line_count):
    fields = {"request_base64", "source_positions", "source_position_kind", "source_record_number", "diagnostic"}
    if type(record) is not dict or set(record) != fields:
        raise WorkerProtocolError("Structure worker record has missing or unexpected fields.")
    if type(record["source_record_number"]) is not int or record["source_record_number"] != number:
        raise WorkerProtocolError("Structure worker records differ from the requested order or source identities.")
    raw = decode_source_bytes(record["request_base64"])
    try:
        admitted = prepare_json(raw)
    except InputRejected as cause:
        raise WorkerProtocolError("Structure worker returned an invalid executable coordinate request.") from cause
    positions = record["source_positions"]
    if (
        type(positions) is not list
        or len(positions) != len(admitted.source_positions)
        or any(type(n) is not int for n in positions)
    ):
        raise WorkerProtocolError("Structure worker must return one integer source position for each atom.")
    if source_format == "json":
        valid = raw == source and positions == list(admitted.source_positions) and record["source_position_kind"] == "atom_index"
    elif source_format in ("mol", "sdf"):
        # Qualified native readers preserve MOL atom rows, numbered within each
        # selected record. Atom-map properties and source line numbers are not IDs.
        valid = record["source_position_kind"] == "atom_id" and positions == list(range(1, len(positions) + 1))
    elif source_format == "pdb":
        valid = (
            record["source_position_kind"] == "atom_serial"
            and len(set(positions)) == len(positions)
            and all(1 <= number <= 99999 for number in positions)
        )
    elif source_format == "mol2":
        valid = (
            record["source_position_kind"] == "atom_id"
            and len(set(positions)) == len(positions)
            and all(1 <= number <= MAX_ATOM_ID for number in positions)
        )
    else:
        valid = (
            record["source_position_kind"] == "line_number"
            and positions == sorted(set(positions))
            and all(1 <= n <= line_count for n in positions)
        )
    if not valid:
        raise WorkerProtocolError("Structure worker returned changed direct JSON or invalid source atom locations.")
    return admitted
