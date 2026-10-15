"""Acquire the declared Zenodo release without publishing unverified bytes."""

# Acquisition precedent: declared Zenodo identity, resumable partials, and checks
# before publication. The ZIP bounds and selected-member receipts are local.
# https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_weights.py
# SECS verifies the whole archive before admitting a selected member; retain
# that assurance here even though ZIP selection uses a materialized archive.
# https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/tools/extract_checkpoint.py

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import argparse
import ctypes
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import struct
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path

# Keep artifact and producer hashing, downloads, and member copies bounded to
# the same fixed read budget; this is not an operator-selectable release fact.
# repo-check: allow-module-constant
CHUNK_BYTES = 1024 * 1024
# Bound the ZIP directory read before ZipFile allocates its metadata table.
# repo-check: allow-module-constant
MAX_DIRECTORY_BYTES = 8 * 1024 * 1024
# Bound both trailer-declared and actually parsed directory membership.
# repo-check: allow-module-constant
MAX_MEMBERS = 10000


def load_declaration(path: Path) -> dict:
    """Load the closed release declaration and bind each published archive identity.

    artifacts/release.json defines the admitted metadata fields. Reject additions
    before consumers can silently ignore a new acquisition or provenance claim.
    """
    document = json.loads(path.read_text())
    root_fields = {"record_id", "doi", "version_index", "publication_date", "metadata_url", "files"}
    if not isinstance(document, dict) or set(document) != root_fields:
        raise ValueError("Cannot load release: fields must match artifacts/release.json")
    if not isinstance(document["files"], list):
        raise ValueError("Cannot load release: files must be a list of archive declarations")
    record = document["record_id"]
    if not isinstance(record, str) or not re.fullmatch(r"[0-9]+", record):
        raise ValueError("Cannot load release: record_id must identify a Zenodo record")
    if document["doi"] != f"10.5281/zenodo.{record}":
        raise ValueError("Cannot load release: DOI does not identify record_id")
    names = set()
    for item in document["files"]:
        if not isinstance(item, dict) or set(item) != {"name", "url", "size", "md5"}:
            raise ValueError("Cannot load release: archive fields must be name, url, size, and md5")
        name = item["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", name):
            raise ValueError(f"Cannot acquire archive with unsafe file name {name!r}")
        if name in names:
            raise ValueError(f"Cannot load release: duplicate archive {name!r}")
        names.add(name)
        if not (type(item["size"]) is int and item["size"] > 0):
            raise ValueError(f"Cannot acquire {name!r}: size must be a positive byte count")
        if not re.fullmatch(r"[0-9a-f]{32}", item["md5"]):
            raise ValueError(f"Cannot acquire {name!r}: published MD5 must contain 32 hex digits")
        expected_url = f"https://zenodo.org/api/records/{record}/files/{name}/content"
        if item["url"] != expected_url:
            raise ValueError(f"Cannot acquire {name!r}: URL does not name the declared record file")
    if not names:
        raise ValueError("Cannot load release: no files are declared")
    return document


@contextmanager
def regular_file(path: Path, flags: int):
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Cannot use {str(path)!r}: artifact must be a regular file")
        with os.fdopen(descriptor, "r+b" if flags & os.O_RDWR else "rb", closefd=False) as stream:
            yield stream
    finally:
        os.close(descriptor)


def verify(path: Path, declaration: dict) -> dict:
    """Verify published size/MD5 and record SHA-256 of the same opened bytes."""
    with regular_file(path, os.O_RDONLY) as stream:
        return verify_stream(stream, declaration)


def verify_stream(stream, declaration: dict) -> dict:
    """Keep verification on the descriptor that archive consumption will reuse.

    The archive identity's size must be a positive built-in int, as admitted by
    load_declaration. Callers supply this admitted byte-count contract.
    """
    actual_size = os.fstat(stream.fileno()).st_size
    name = declaration["name"]
    if actual_size != declaration["size"]:
        raise ValueError(f"Cannot verify {name!r}: {actual_size} bytes; published size is {declaration['size']}")
    stream.seek(0)
    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    count = 0
    while chunk := stream.read(CHUNK_BYTES):
        count += len(chunk)
        if not (count <= declaration["size"]):
            raise ValueError(f"Cannot verify {name!r}: file grew beyond its published size")
        md5.update(chunk)
        sha256.update(chunk)
    if count != declaration["size"] or md5.hexdigest() != declaration["md5"]:
        raise ValueError(f"Cannot verify {name!r}: bytes do not match published MD5 {declaration['md5']}")
    return {"size": count, "md5": md5.hexdigest(), "sha256": sha256.hexdigest()}


class HttpsRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if urllib.parse.urlsplit(new_url).scheme != "https":
            raise ValueError("Cannot download archive: server redirected outside HTTPS")
        return super().redirect_request(request, fp, code, message, headers, new_url)


def open_url(request, **kwargs):
    return urllib.request.build_opener(HttpsRedirects()).open(request, **kwargs)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def download(declaration: dict, directory: Path, *, opener=None) -> None:
    """Resume into a partial, then publish by an atomic non-replacing hard link.

    The archive identity's size must be a positive built-in int, as admitted by
    load_declaration. Callers supply this admitted byte-count contract.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / declaration["name"]
    partial = directory / (declaration["name"] + ".part")
    with regular_file(directory / (declaration["name"] + ".lock"), os.O_RDWR | os.O_CREAT) as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if target.exists() or target.is_symlink():
            verify(target, declaration)
            return
        with regular_file(partial, os.O_RDWR | os.O_CREAT) as destination:
            offset = os.fstat(destination.fileno()).st_size
            if not (offset <= declaration["size"]):
                raise ValueError(f"Cannot resume {partial.name!r}: partial exceeds published size")
            if offset < declaration["size"]:
                request = urllib.request.Request(
                    declaration["url"],
                    headers={
                        "User-Agent": "nmrnet-repro/1",
                        "Accept-Encoding": "identity",
                        **({"Range": f"bytes={offset}-"} if offset else {}),
                    },
                )
                with (opener or open_url)(request, timeout=60) as source:
                    expected_range = f"bytes {offset}-{declaration['size'] - 1}/{declaration['size']}"
                    if offset and (source.status != 206 or source.headers.get("Content-Range") != expected_range):
                        raise ValueError(
                            f"Cannot resume {partial.name!r}: server did not honor Range {expected_range!r}; partial preserved"
                        )
                    if not offset and source.status != 200:
                        raise ValueError(f"Cannot download {target.name!r}: expected HTTP 200, received {source.status}")
                    # RFC 9110 section 8.4 requires listing applied HTTP content codings:
                    # https://www.rfc-editor.org/rfc/rfc9110.html#section-8.4
                    # Identity means no coding; ranges would otherwise address coded bytes:
                    # https://www.rfc-editor.org/rfc/rfc9110.html#section-12.5.3
                    # https://www.rfc-editor.org/rfc/rfc9110.html#section-14.1.2
                    # Literal identity remains accepted compatibility; section 8.4 says it
                    # SHOULD NOT be sent as Content-Encoding. Size and digest still gate publication.
                    # silent-defaults: allow-contract -- Absent Content-Encoding denotes no additional HTTP coding; proof=tests/test_artifacts.py::ArtifactTests.test_http_content_coding_preserves_declared_archive_offsets
                    if source.headers.get("Content-Encoding", "identity") != "identity":
                        raise ValueError(f"Cannot download {target.name!r}: encoded response cannot preserve archive offsets")
                    destination.seek(offset)
                    while chunk := source.read(min(CHUNK_BYTES, declaration["size"] - offset + 1)):
                        if not (offset + len(chunk) <= declaration["size"]):
                            raise ValueError(
                                f"Cannot download {target.name!r}: response exceeds published size; partial preserved"
                            )
                        destination.write(chunk)
                        offset += len(chunk)
                destination.flush()
            os.fsync(destination.fileno())
        verify(partial, declaration)
        os.link(partial, target, follow_symlinks=False)
        fsync_directory(directory)
        partial.unlink()
        fsync_directory(directory)


def check_zip_directory_bound(stream) -> None:
    """Bound ZIP/ZIP64 metadata before ZipFile allocates its directory table."""
    # CPython 3.12.12 ZipFile._RealGetContents reads size_cd into memory at once;
    # enforce the metadata limit before constructing ZipFile.
    # https://github.com/python/cpython/blob/v3.12.12/Lib/zipfile/__init__.py#L1429-L1456
    stream.seek(0, os.SEEK_END)
    length = stream.tell()
    stream.seek(max(0, length - 65557))
    tail = stream.read(65557)
    end = tail.rfind(b"PK\x05\x06")
    if not (end >= 0 and len(tail) - end >= 22):
        raise ValueError("Cannot inspect ZIP: complete end-of-directory record is missing")
    _, disk, start_disk, disk_entries, entries, size, offset, comment = struct.unpack_from("<4s4H2LH", tail, end)
    if disk or start_disk or disk_entries != entries or end + 22 + comment != len(tail):
        raise ValueError("Cannot inspect ZIP: split archives or incomplete directory trailers are not supported")
    absolute_end = length - len(tail) + end
    locator = b""
    if absolute_end >= 20:
        stream.seek(absolute_end - 20)
        locator = stream.read(20)
    # ZipFile always honors an adjacent ZIP64 locator, even without sentinels
    # in the ordinary directory record. Bound the same metadata it will read.
    # CPython 3.12.12 _EndRecData calls _EndRecData64 in both trailer branches:
    # https://github.com/python/cpython/blob/v3.12.12/Lib/zipfile/__init__.py#L238-L353
    if locator[:4] == b"PK\x06\x07":
        _signature, disk, location, disks = struct.unpack("<4sLQL", locator)
        if disk or disks != 1 or location != absolute_end - 76:
            raise ValueError("Cannot inspect ZIP64: directory locator does not name one archive")
        stream.seek(absolute_end - 76)
        record = stream.read(56)
        if len(record) != 56 or record[:4] != b"PK\x06\x06":
            raise ValueError("Cannot inspect ZIP64: complete directory record is missing")
        _, record_size, _, _, disk, start_disk, disk_entries, entries, size, offset = struct.unpack("<4sQ2H2L4Q", record)
        if record_size != 44 or disk or start_disk or disk_entries != entries:
            raise ValueError("Cannot inspect ZIP64: extended directory records and split archives are not supported")
    elif entries == 65535 or size == 0xFFFFFFFF or offset == 0xFFFFFFFF:
        raise ValueError("Cannot inspect ZIP64: complete directory locator is missing")
    # struct.unpack supplies integer metadata, so these accepted bounds are the
    # exact inverse of the oversized-directory rejection for ZIP and ZIP64.
    if not (size <= MAX_DIRECTORY_BYTES and entries <= MAX_MEMBERS):
        raise ValueError(f"Cannot inspect ZIP: directory exceeds {MAX_DIRECTORY_BYTES} bytes or {MAX_MEMBERS} members")
    if not (offset + size <= length):
        raise ValueError("Cannot inspect ZIP: directory extends beyond the archive")
    stream.seek(0)


def safe_member_name(name: str) -> str:
    trimmed = name[:-1] if name.endswith("/") else name
    if (
        not trimmed
        or "\\" in name
        or ":" in name
        or any(ord(character) < 32 for character in name)
        or any(part in ("", ".", "..") for part in trimmed.split("/"))
    ):
        raise ValueError(f"Cannot inspect ZIP: unsafe member path {name!r}")
    return trimmed


def archive_members(archive: zipfile.ZipFile) -> dict:
    if not (len(archive.infolist()) <= MAX_MEMBERS):
        raise ValueError(f"Cannot inspect ZIP: actual directory exceeds {MAX_MEMBERS} members")
    members = {}
    for info in archive.infolist():
        name = safe_member_name(info.orig_filename)
        # ZipExtFile bounds DEFLATE output, but expands BZIP2/LZMA without
        # an output limit before truncating to the member's claimed size.
        # CPython 3.12.12 ZipExtFile._read1 and LZMADecompressor.decompress:
        # https://github.com/python/cpython/blob/v3.12.12/Lib/zipfile/__init__.py#L1065-L1095
        # https://github.com/python/cpython/blob/v3.12.12/Lib/zipfile/__init__.py#L694-L719
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise ValueError(
                f"Cannot inspect ZIP: member {name!r} uses compression method {info.compress_type}; only STORED and DEFLATED are supported"
            )
        mode = stat.S_IFMT(info.external_attr >> 16)
        if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ValueError(f"Cannot inspect ZIP: member {name!r} is not a regular file or directory")
        if mode and (mode == stat.S_IFDIR) != info.is_dir():
            raise ValueError(f"Cannot inspect ZIP: directory mode and path disagree for {name!r}")
        if name in members:
            raise ValueError(f"Cannot inspect ZIP: duplicate member {name!r}")
        members[name] = info
    return members


def inspect_archive(path: Path, declaration: dict) -> list:
    with regular_file(path, os.O_RDONLY) as source:
        verify_stream(source, declaration)
        check_zip_directory_bound(source)
        with zipfile.ZipFile(source) as archive:
            return [
                {"name": info.filename, "size": info.file_size, "compressed_size": info.compress_size}
                for info in archive_members(archive).values()
            ]


def publish_directory(staged: Path, output: Path) -> None:
    """Linux renameat2 publishes a complete directory without replacing any path."""
    libc = ctypes.CDLL(None, use_errno=True)
    rename = libc.renameat2
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staged), -100, os.fsencode(output), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(output))


def extract(path: Path, declaration: dict, selection: dict, output: Path) -> None:
    """Extract exact regular members offline, publishing one complete provenance set."""
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"Cannot prepare artifacts: output {str(output)!r} already exists")
    names = selection["members"]
    limit = selection["max_bytes"]
    if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
        raise ValueError("Cannot extract artifacts: selection must list exact member paths")
    if not (len(names) == len(set(names)) and type(limit) is int and limit > 0):
        raise ValueError("Cannot extract artifacts: members must be unique and max_bytes must be positive")
    for name in names:
        safe_member_name(name)
        if name == "provenance.json" or name.startswith("provenance.json/"):
            raise ValueError("Cannot extract artifacts: provenance.json is reserved for the preparation receipt")
    output.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{output.name}.partial-", dir=output.parent))
    try:
        with regular_file(path, os.O_RDONLY) as source:
            identity = verify_stream(source, declaration)
            check_zip_directory_bound(source)
            with zipfile.ZipFile(source) as archive:
                members = archive_members(archive)
                for name in names:
                    if name not in members:
                        raise ValueError(f"Cannot extract artifacts: required member {name!r} is missing")
                    if members[name].is_dir():
                        raise ValueError(f"Cannot extract artifacts: required member {name!r} is a directory")
                total = sum(members[name].file_size for name in names)
                if not (total <= limit):
                    raise ValueError(f"Cannot extract artifacts: selected size {total} bytes exceeds limit {limit}")
                # Bind the mounted producer, including uncommitted edits. The
                # baseline producer uses the same descriptor-bound byte identity:
                # tools/baseline_prepare.py::file_identity / execution_identity.
                producer_sha256 = hashlib.sha256()
                producer_size = 0
                with regular_file(Path(__file__), os.O_RDONLY) as producer:
                    while chunk := producer.read(CHUNK_BYTES):
                        producer_size += len(chunk)
                        producer_sha256.update(chunk)
                receipt = {
                    "archive": declaration,
                    "archive_sha256": identity["sha256"],
                    "members": {},
                    "producer_sources": {"tools/artifacts.py": {"size": producer_size, "sha256": producer_sha256.hexdigest()}},
                }
                written = 0
                for name in names:
                    destination = staged / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    sha256 = hashlib.sha256()
                    count = 0
                    with archive.open(members[name]) as member, destination.open("xb") as target:
                        while chunk := member.read(CHUNK_BYTES):
                            count += len(chunk)
                            written += len(chunk)
                            # The selection limit is an admitted positive int;
                            # ZipFile parsed the member size as an unsigned int.
                            if not (written <= limit and count <= members[name].file_size):
                                raise ValueError(f"Cannot extract {name!r}: decompressed bytes exceed the declared limit")
                            sha256.update(chunk)
                            target.write(chunk)
                        if count != members[name].file_size:
                            raise ValueError(f"Cannot extract {name!r}: member ended before its declared size")
                        target.flush()
                        os.fsync(target.fileno())
                    receipt["members"][name] = {"size": count, "sha256": sha256.hexdigest()}
        with (staged / "provenance.json").open("x") as target:
            json.dump(receipt, target, indent=2, sort_keys=True)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        for root, _, _ in os.walk(staged, topdown=False):
            fsync_directory(Path(root))
        publish_directory(staged, output)
        fsync_directory(output.parent)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("download", "check", "inspect", "extract"))
    parser.add_argument("--declaration", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    release = load_declaration(args.declaration)
    selected = [item for item in release["files"] if item["name"] == args.name]
    if not selected:
        parser.error(f"archive {args.name!r} is not declared")
    for item in selected:
        if args.command == "download":
            download(item, args.directory)
        elif args.command == "inspect":
            sys.stdout.write(json.dumps(inspect_archive(args.directory / item["name"], item), indent=2) + "\n")
        elif args.command == "extract":
            if args.selection is None or args.output is None:
                parser.error("extract requires --selection and --output")
            extract(args.directory / item["name"], item, json.loads(args.selection.read_text()), args.output)
        else:
            sys.stdout.write(
                json.dumps({"name": item["name"], **verify(args.directory / item["name"], item)}, sort_keys=True) + "\n"
            )


if __name__ == "__main__":
    main()
