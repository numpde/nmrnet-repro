#!/usr/bin/env python3
"""Verify the pinned acquisition bootstrap or refresh it from its release archive."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "requirements/provider-core-release.json"
SNAPSHOT = ROOT / "tools/bootstrap/acquisition"


def inventory(record):
    files = record["acquisition_source_files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("the acquisition release inventory is empty")
    for name in files:
        if not isinstance(name, str):
            raise ValueError("the release inventory has an invalid path")
        path = PurePosixPath(name)
        if not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != name:
            raise ValueError("the release inventory has an invalid path")
    return files


def verify_file(name, data, entry):
    if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise ValueError(f"release file {name} differs from its recorded bytes")


def snapshot_files(snapshot):
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise ValueError("the bootstrap snapshot is missing or is a symlink")
    files = set()
    def fail_walk(error):
        raise error

    for current, directories, names in os.walk(snapshot, followlinks=False, onerror=fail_walk):
        parent = Path(current)
        if any((parent / name).is_symlink() for name in directories):
            raise ValueError("the bootstrap snapshot contains a symlinked directory")
        for name in names:
            path = parent / name
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError("the bootstrap snapshot contains a non-regular file")
            files.add(path.relative_to(snapshot).as_posix())
    return files


def verify_snapshot(snapshot, entries):
    if snapshot_files(snapshot) != set(entries):
        raise ValueError("the bootstrap files differ from the release inventory")
    for name, entry in entries.items():
        path = snapshot / name
        verify_file(name, path.read_bytes(), entry)
        if stat.S_IMODE(path.stat().st_mode) & 0o111 != entry["mode"] & 0o111:
            raise ValueError(f"release file {name} has the wrong executable bits")


def refresh(archive_path, snapshot, record, entries):
    archive_bytes = archive_path.read_bytes()
    if (archive_path.name not in record["files"]
            or not archive_path.name.startswith("acquisition-owner-")
            or not archive_path.name.endswith(".tar.gz")
            or hashlib.sha256(archive_bytes).hexdigest() != record["files"][archive_path.name]):
        raise ValueError("the acquisition archive differs from the release pin")
    sources = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive:
            if (not member.isfile() or member.name not in entries or member.name in sources
                    or member.mode != entries[member.name]["mode"]):
                raise ValueError("the archive members differ from the release inventory")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("a release archive member cannot be read")
            data = stream.read()
            verify_file(member.name, data, entries[member.name])
            sources[member.name] = data
    if set(sources) != set(entries):
        raise ValueError("the archive is missing release files")
    if snapshot.parent.is_symlink():
        raise ValueError("the bootstrap parent is a symlink")
    if snapshot.exists() or snapshot.is_symlink():
        if snapshot_files(snapshot) - set(entries):
            raise ValueError("the existing bootstrap contains unexpected files")
        for name in entries:
            target = snapshot / name
            if target.exists() and target.stat().st_nlink != 1:
                raise ValueError("the existing bootstrap contains a hardlinked file")
    snapshot.mkdir(parents=True, exist_ok=True)
    try:
        for name, data in sources.items():
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(entries[name]["mode"])
    except OSError as error:
        raise OSError("bootstrap refresh failed; the snapshot may be partially updated") from error
    verify_snapshot(snapshot, entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="refresh from the pinned release archive")
    args = parser.parse_args()
    record = json.loads(RECORD.read_text())
    entries = inventory(record)
    if args.archive:
        refresh(args.archive, SNAPSHOT, record, entries)
    else:
        verify_snapshot(SNAPSHOT, entries)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, EOFError) as error:
        print(f"Cannot prepare acquisition bootstrap: {error}", file=sys.stderr)
        raise SystemExit(1) from None
