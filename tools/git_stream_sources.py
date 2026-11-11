"""Bind streamed build sources to their captured Git blob identities."""

import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath

RECORD = re.compile(rb"(100644|100755) blob ([0-9a-f]{40})\t([A-Za-z0-9_./-]+)")


def source_allowlist(context, inventories):
    """Return the streamer's source entries for captured Git tree inventories."""
    context = Path(context)
    if not context.is_absolute() or ".." in context.parts:
        raise ValueError("Git source context must be an absolute path.")
    root = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    entries = {}
    try:
        for part in context.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root)
            os.close(root)
            root = child
        for inventory, prefix_text, expected_inventory in inventories:
            prefix = PurePosixPath(prefix_text)
            if prefix.is_absolute() or ".." in prefix.parts or str(prefix) != prefix_text:
                raise ValueError("Git source prefix must be repository-relative.")
            descriptor = os.open(inventory, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise ValueError("Git source inventory must be a regular file.")
            with os.fdopen(descriptor, "rb") as stream:
                records = stream.read()
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_inventory) or (
                "sha256:" + hashlib.sha256(records).hexdigest() != expected_inventory
            ):
                raise ValueError("Git source inventory differs from the captured image identity.")
            if not records or not records.endswith(b"\0"):
                raise ValueError("Git source inventory must contain complete NUL-delimited records.")
            for record in records[:-1].split(b"\0"):
                match = RECORD.fullmatch(record)
                if match is None:
                    raise ValueError("Git source inventory contains an invalid blob record.")
                mode, object_id, raw_name = match.groups()
                name = raw_name.decode("ascii")
                relative = prefix / name
                if (
                    name in (".", "..")
                    or name.startswith("/")
                    or any(part in (".", "..") for part in relative.parts)
                    or str(PurePosixPath(name)) != name
                ):
                    raise ValueError("Git source inventory contains a noncanonical path.")
                destination = relative.as_posix()
                if destination in entries:
                    raise ValueError(f"Git source destination {destination!r} is duplicated.")
                descriptor = os.dup(root)
                try:
                    for part in relative.parts[:-1]:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                        os.close(descriptor)
                        descriptor = child
                    file_descriptor = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                finally:
                    os.close(descriptor)
                if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                    os.close(file_descriptor)
                    raise ValueError(f"Captured Git source {destination!r} must be a regular file.")
                with os.fdopen(file_descriptor, "rb") as stream:
                    metadata = os.fstat(stream.fileno())
                    executable = mode == b"100755"
                    if not stat.S_ISREG(metadata.st_mode) or bool(metadata.st_mode & 0o111) != executable:
                        raise ValueError(f"Captured Git source {destination!r} has an unexpected type or mode.")
                    git_hash = hashlib.sha1(f"blob {metadata.st_size}\0".encode(), usedforsecurity=False)
                    sha256 = hashlib.sha256()
                    size = 0
                    while block := stream.read(1024 * 1024):
                        size += len(block)
                        git_hash.update(block)
                        sha256.update(block)
                    if size != metadata.st_size or git_hash.hexdigest().encode() != object_id:
                        raise ValueError(f"Captured Git source {destination!r} differs from its committed blob.")
                entries[destination] = {
                    "path": str(context / destination),
                    "destination": destination,
                    "sha256": sha256.hexdigest(),
                    "executable": executable,
                }
    finally:
        os.close(root)
    return [entries[name] for name in sorted(entries)]


def main():
    if len(sys.argv) < 5 or (len(sys.argv) - 2) % 3 != 0:
        raise ValueError("usage: git_stream_sources.py CONTEXT INVENTORY PREFIX INVENTORY_SHA256 [...]")
    inventories = list(zip(sys.argv[2::3], sys.argv[3::3], sys.argv[4::3], strict=True))
    sys.stdout.write(json.dumps(source_allowlist(sys.argv[1], inventories), sort_keys=True) + "\n")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        sys.stderr.write(f"Cannot prepare streamed Git inputs: {error}\n")
        raise SystemExit(2) from error
