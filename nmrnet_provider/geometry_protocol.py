"""Carry source bytes into the reader and frozen geometry back, without model I/O.

The frame and lifecycle owners are shared with prediction. Source identity and
unit provenance come from the caller's exact bytes/options, not worker-supplied
copies. Prepared bytes, source locations and protected reader diagnostics return
over the wire. The existing whole-frame limit includes those diagnostics.
This is note 003 slice 2a; native reader routes require separate qualification.
"""

from tools.container_guard import require_container

require_container()

import base64
import binascii
import hashlib

from nmrnet_inference.predict import MAX_INPUT_BYTES
from nmrnet_provider.input_geometry import (
    InputRejected,
    PreparedGeometry,
    coordinate_conversion,
    prepare_json,
    prepare_table,
)
from nmrnet_provider.worker_protocol import WorkerProtocolError
from nmrnet_provider.xyz import MAX_RECORDS, prepare_xyz


def diagnostic_text(value):
    """Admit optional private text without clipping the reader's captured evidence."""
    if value is not None and type(value) is not str:
        raise WorkerProtocolError("Structure worker diagnostic must be text or null.")
    return value


def _options(source_format, source_unit, selection):
    if source_format not in ("json", "table", "xyz"):
        raise InputRejected("Cannot read structure: choose json, table or xyz; native file readers are not enabled.")
    coordinate_conversion(source_unit)
    if source_format == "json" and source_unit != "angstrom":
        raise InputRejected("Cannot read direct coordinate JSON: its source unit must be angstrom.")
    if source_format != "xyz":
        if selection is not None:
            raise InputRejected("Cannot select records: JSON and coordinate tables contain exactly one geometry; omit selection.")
    elif selection != "all" and not (
        type(selection) is tuple
        and 1 <= len(selection) <= MAX_RECORDS
        and all(type(number) is int and 1 <= number <= MAX_RECORDS for number in selection)
        and len(set(selection)) == len(selection)
    ):
        raise InputRejected("Cannot select XYZ records: choose all or unique positive record numbers, up to 128.")


def _source_bytes(encoded):
    if type(encoded) is not str or len(encoded) > 4 * ((MAX_INPUT_BYTES + 2) // 3):
        raise WorkerProtocolError("Cannot read structure request: encoded source exceeds the 1 MiB source limit or is not text.")
    try:
        source = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as cause:
        raise WorkerProtocolError("Cannot read structure request: source is not valid base64.") from cause
    if len(source) > MAX_INPUT_BYTES:
        raise WorkerProtocolError("Cannot read structure request: decoded source exceeds the 1 MiB source limit.")
    return source


def source_request(source, *, source_format, source_unit, selection):
    """Admit explicit reading options and the source-byte bound before dispatch."""
    if type(source) is not bytes:
        raise TypeError("Cannot read structure: source must be immutable bytes.")
    if len(source) > MAX_INPUT_BYTES:
        raise InputRejected("Cannot read structure: source exceeds the 1 MiB byte limit.")
    _options(source_format, source_unit, selection)
    return {
        "operation": "read_geometry",
        "source_base64": base64.b64encode(source).decode("ascii"),
        "source_format": source_format,
        "source_unit": source_unit,
        "selection": list(selection) if type(selection) is tuple else selection,
    }


def read_source(request):
    """Invoke one existing reader; the complete selected batch precedes any reply."""
    if (
        set(request) != {"operation", "source_base64", "source_format", "source_unit", "selection"}
        or request["operation"] != "read_geometry"
    ):
        raise WorkerProtocolError("Structure worker accepts only read_geometry with its exact source and option fields.")
    selection = tuple(request["selection"]) if type(request["selection"]) is list else request["selection"]
    source_format, source_unit = request["source_format"], request["source_unit"]
    _options(source_format, source_unit, selection)
    source = _source_bytes(request["source_base64"])
    if source_format == "json":
        geometries = (prepare_json(source),)
    elif source_format == "table":
        geometries = (prepare_table(source, source_unit=source_unit),)
    else:
        geometries = prepare_xyz(source, source_unit=source_unit, selection=selection)
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
    rejects malformed locations, wrong selections, changed direct JSON and invalid
    executable requests; it does not reparse native source files in the controller.
    Reader fixture qualification establishes scientific correspondence; wire
    validation alone cannot establish that a parser interpreted the source correctly.
    """
    if set(reply) != {"kind", "allocation", "records"} or reply["kind"] != "geometries":
        raise WorkerProtocolError("Structure worker returned no complete geometry collection.")
    records = reply["records"]
    if type(records) is not list or not 1 <= len(records) <= MAX_RECORDS:
        raise WorkerProtocolError("Structure worker must return between 1 and 128 geometry records.")
    expected_numbers = tuple(range(1, len(records) + 1)) if selection == "all" else selection
    if source_format != "xyz":
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
    raw = _source_bytes(record["request_base64"])
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
    else:
        valid = (
            record["source_position_kind"] == "line_number"
            and positions == sorted(set(positions))
            and all(1 <= n <= line_count for n in positions)
        )
    if not valid:
        raise WorkerProtocolError("Structure worker returned changed direct JSON or invalid source atom locations.")
    return admitted
