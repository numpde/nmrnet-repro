"""Verify or refresh the pinned bootstrap during quiescent source maintenance."""

import argparse
import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

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
    expected_directories = {
        parent.as_posix() for name in entries for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")
    }
    for current, directories, _files in os.walk(snapshot, followlinks=False):
        for name in directories:
            if (Path(current) / name).relative_to(snapshot).as_posix() not in expected_directories:
                raise ValueError("the bootstrap directories differ from the release inventory")
    if snapshot_files(snapshot) != set(entries):
        raise ValueError("the bootstrap files differ from the release inventory")
    for name, entry in entries.items():
        path = snapshot / name
        verify_file(name, path.read_bytes(), entry)
        if stat.S_IMODE(path.stat().st_mode) & 0o111 != entry["mode"] & 0o111:
            raise ValueError(f"release file {name} has the wrong executable bits")


def refresh(archive_path, snapshot, record, entries, record_output, record_bytes):
    if snapshot.parent.is_symlink():
        raise ValueError("the bootstrap parent is a symlink")
    if snapshot.exists() or snapshot.is_symlink():
        previous = inventory(json.loads(record_output.read_bytes()))
        for name in snapshot_files(snapshot):
            if (snapshot / name).stat().st_nlink != 1:
                raise ValueError("the existing bootstrap contains a hardlinked file")
        verify_snapshot(snapshot, previous)
    archive_bytes = archive_path.read_bytes()
    if (
        archive_path.name not in record["files"]
        or not archive_path.name.startswith("acquisition-owner-")
        or not archive_path.name.endswith(".tar.gz")
        or hashlib.sha256(archive_bytes).hexdigest() != record["files"][archive_path.name]
    ):
        raise ValueError("the acquisition archive differs from the release pin")
    sources = {}
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
        for member in archive:
            if (
                not member.isfile()
                or member.name not in entries
                or member.name in sources
                or member.mode != entries[member.name]["mode"]
            ):
                raise ValueError("the archive members differ from the release inventory")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("a release archive member cannot be read")
            data = stream.read()
            verify_file(member.name, data, entries[member.name])
            sources[member.name] = data
    if set(sources) != set(entries):
        raise ValueError("the archive is missing release files")
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".acquisition-", dir=snapshot.parent))
    backup = None
    staged_record = None
    try:
        with tempfile.NamedTemporaryFile(dir=record_output.parent, prefix=".acquisition-record-", delete=False) as output:
            staged_record = Path(output.name)
            output.write(record_bytes)
            output.flush()
            os.fchmod(output.fileno(), 0o644)
            os.fsync(output.fileno())
        for name, data in sources.items():
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(entries[name]["mode"])
        for directory, _children, _files in os.walk(stage):
            Path(directory).chmod(0o755)
        verify_snapshot(stage, entries)
        if snapshot.exists():
            backup = Path(tempfile.mkdtemp(prefix=".acquisition-previous-", dir=snapshot.parent))
            backup.rmdir()
            snapshot.rename(backup)
        try:
            stage.rename(snapshot)
            staged_record.replace(record_output)
        except BaseException:
            if not stage.exists() and snapshot.exists():
                snapshot.rename(stage)
            if backup is not None:
                backup.rename(snapshot)
                backup = None
            raise
        if backup is not None:
            try:
                shutil.rmtree(backup)
            except OSError as error:
                raise OSError(f"Bootstrap replacement published; cannot remove previous snapshot at {backup}.") from error
            backup = None
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        if staged_record is not None:
            staged_record.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="refresh from the pinned release archive")
    parser.add_argument("--issuer", type=Path, help="incoming release record for an explicit refresh")
    args = parser.parse_args()
    if args.archive is None:
        if args.issuer is not None:
            raise ValueError("an incoming issuer requires an acquisition archive")
        record = json.loads(RECORD.read_bytes())
        verify_snapshot(SNAPSHOT, inventory(record))
        return
    if args.issuer is None:
        raise ValueError("refresh requires the incoming release record")
    record_bytes = args.issuer.read_bytes()
    record = json.loads(record_bytes)
    refresh(args.archive, SNAPSHOT, record, inventory(record), RECORD, record_bytes)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, EOFError) as error:
        sys.stderr.write(f"Cannot prepare acquisition bootstrap: {error}\n")
        raise SystemExit(1) from None
