"""Inspect and decode an immutable Upload inside the supervised structure reader.

Adapted narrowly from SECS7356c354368f2355d3ab5cef00cf48c67cea9c2c,
src/secs_inference/provider/source_access.py::_preflight_archive,
_indexed_member, _read_chunk and _read_stream (SHA-256
04911c122000b0246b844ef3b2567d4e5647effabef4eb0809b2b7bbfa62ef86).
Keep its bounded index, exact duplicate-aware selection and decoder-only error
translation. No paths are extracted; member names are opaque logical labels.
Acquisition and Job membership remain provider-core responsibilities. The existing
preparation deadline and worker-call limit bound repeated inspection/decoding.
"""

from tools.container_guard import require_container

require_container()

import json
import stat
import struct
import zlib
from contextlib import contextmanager
from io import BytesIO
from lzma import LZMAError
from zipfile import BadZipFile, ZipFile, is_zipfile

from nmrnet_inference.predict import MAX_INPUT_BYTES
from nmrnet_provider.input_geometry import InputRejected

MAX_MEMBERS = 4096
# Smaller than SECS's discovery listing: NMRNet embeds this metadata in a
# bounded interpreter request (the qualified profile admits 64 KiB total).
# Preserve room for the Job, tool declarations and previous correction turns.
MAX_LISTING_BYTES = 16384


def is_archive(source):
    """Recognize truncated ordinary ZIP headers too, without format fallback."""
    return source.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"PK\x06\x06")) or is_zipfile(BytesIO(source))


def inspect_upload(source):
    """List declared member sizes; no member content is decompressed here."""
    if not is_archive(source):
        return {"container": "direct", "members": []}
    with _archive(source) as archive:
        if any(info.orig_filename != info.filename for info in archive.infolist()):
            raise InputRejected("Cannot inspect this ZIP: a member name contains a NUL character; repackage with valid names.")
        entries = [{"name": info.filename, "byte_length": info.file_size} for info in archive.infolist() if not info.is_dir()]
    inventory = {"container": "zip", "members": entries}
    if len(json.dumps(inventory, ensure_ascii=True).encode("ascii")) > MAX_LISTING_BYTES:
        raise InputRejected(
            "Cannot inspect this ZIP: its member listing exceeds 16384 bytes; "
            "select an exact known member name or attach a smaller archive."
        )
    return inventory


def select_member(source, member):
    """Read one exact regular member completely, with CRC and actual-byte limits."""
    if type(member) is not str or not member:
        raise InputRejected("Cannot select ZIP content: inspect_upload first, then supply an exact nonempty member name.")
    with _archive(source) as archive:
        info = _member(archive, member)
        try:
            stream = archive.open(info)
        except (BadZipFile, NotImplementedError, RuntimeError, UnicodeDecodeError) as cause:
            raise _decoder_rejection(
                "Cannot read the selected ZIP member: decoding or integrity checking failed.", cause
            ) from cause
        with stream:
            contents = _read_member(stream)
        if len(contents) != info.file_size:
            raise InputRejected("Cannot read the selected ZIP member: decoded length differs from its declared size.")
        return contents


def _member(archive, name):
    matches = [info for info in archive.infolist() if info.filename == name]
    if not matches:
        raise InputRejected("Cannot select ZIP content: the exact member name is missing; inspect and select an existing name.")
    if len(matches) != 1:
        raise InputRejected("Cannot select ZIP content: the member name is repeated; repackage files with unique names.")
    info = matches[0]
    if info.orig_filename != info.filename:
        raise InputRejected("Cannot select ZIP content: its member name contains a NUL character.")
    mode = info.external_attr >> 16
    if info.is_dir() or stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
        raise InputRejected("Cannot read the selected ZIP member: it is not a regular file.")
    if info.flag_bits & 1:
        raise InputRejected("Cannot read the selected ZIP member: encrypted members are unsupported.")
    if info.file_size > MAX_INPUT_BYTES:
        raise InputRejected("Cannot read the selected ZIP member: it exceeds the 1 MiB member limit; select a smaller file.")
    return info


def _read_member(stream):
    chunks, total = [], 0
    while True:
        try:
            chunk = stream.read(min(65536, MAX_INPUT_BYTES + 1 - total))
        except (BadZipFile, zlib.error, LZMAError, EOFError, OSError) as cause:
            # bz2 reports corrupt compressed input as an errno-less OSError.
            # Filesystem/native errno failures remain operational, not feedback.
            if isinstance(cause, OSError) and cause.errno is not None:
                raise
            raise _decoder_rejection(
                "Cannot read the selected ZIP member: decoding or integrity checking failed.", cause
            ) from cause
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_INPUT_BYTES:
            raise InputRejected("Cannot read the selected ZIP member: decoded content exceeds the 1 MiB member limit.")
        chunks.append(chunk)


@contextmanager
def _archive(source):
    _preflight_archive(source)
    with BytesIO(source) as stream:
        try:
            archive = ZipFile(stream)
        except (BadZipFile, UnicodeDecodeError, NotImplementedError) as cause:
            raise _decoder_rejection(
                "Cannot inspect archive members: the selected Upload is not a readable ZIP or uses an unsupported ZIP version.",
                cause,
            ) from cause
        with archive:
            if len(archive.infolist()) > MAX_MEMBERS:
                raise InputRejected("Cannot inspect this ZIP: it exceeds the 4096-member inspection limit.")
            yield archive


def _decoder_rejection(message, cause):
    """Keep reviewed decoder evidence across IPC in the protected diagnostic field.

    Only the narrowly caught ZIP/decoder exceptions reach this helper. Their text
    can contain private member names; public guidance never includes it, and the
    existing receiving renderer applies its credential redaction policy.
    """
    diagnostic = f"{type(cause).__name__}: {cause}"
    if len(diagnostic) > 8192:
        diagnostic = diagnostic[:8192] + "\n[ZIP decoder diagnostic truncated]"
    return InputRejected(message, diagnostic=diagnostic)


def _preflight_archive(source):
    """Refuse excessive declared counts before ZipFile constructs its index.

    Forged low counts can defer refusal until the post-construction count check.
    The independently admitted 1 MiB source and reader container limit allocation
    in that case; this does not implement a second central-directory parser.
    """
    tail = source[-65557:]
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0 or len(tail) - eocd < 22:
        return
    disk, central_disk, disk_entries, total_entries = struct.unpack_from("<4H", tail, eocd + 4)
    if disk or central_disk or disk_entries != total_entries:
        raise InputRejected("Cannot inspect this ZIP: multi-disk archives are unsupported.")
    if total_entries == 0xFFFF:
        locator = tail.rfind(b"PK\x06\x07", 0, eocd)
        if locator < 0 or len(tail) - locator < 20:
            raise InputRejected("Cannot inspect this ZIP: its ZIP64 entry count is unavailable.")
        zip64_disk, offset, disks = struct.unpack_from("<IQI", tail, locator + 4)
        if zip64_disk or disks != 1:
            raise InputRejected("Cannot inspect this ZIP: multi-disk archives are unsupported.")
        record = source[offset : offset + 56]
        if len(record) < 56 or not record.startswith(b"PK\x06\x06"):
            raise InputRejected("Cannot inspect this ZIP: its ZIP64 entry count is unavailable.")
        total_entries = struct.unpack_from("<Q", record, 32)[0]
    if total_entries > MAX_MEMBERS:
        raise InputRejected("Cannot inspect this ZIP: it exceeds the 4096-member inspection limit.")
