"""Install the pinned Uni-Core source offline after bounded, link-free extraction."""

from tools.container_guard import require_container

require_container()

import argparse
import gzip
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from tools.artifacts import regular_file
from tools.wheelhouse import digest, source_identity

# Bound expanded tar bytes before tarfile parses and allocates member metadata.
# repo-check: allow-module-constant
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
# Bound the admitted member list before extracting files or executing setup.
# repo-check: allow-module-constant
MAX_MEMBERS = 2048


def build(archive_path: Path, declaration_path: Path) -> None:
    """Install declared source only after byte identity and bounded member admission."""
    source = source_identity(declaration_path)
    if archive_path.stat().st_size != source["size"] or digest(archive_path) != source["sha256"]:
        raise ValueError("Cannot build Uni-Core: source archive size or hash differs from its declaration")
    root_name = "Uni-Core-" + source["commit"]
    with tempfile.TemporaryDirectory(prefix="unicore-build-") as temporary:
        scratch = Path(temporary)
        expanded = scratch / "source.tar"
        # Bound decompression before tar headers can allocate metadata or skip large payloads.
        with (
            regular_file(archive_path, os.O_RDONLY) as compressed,
            gzip.GzipFile(fileobj=compressed) as stream,
            expanded.open("xb") as target,
        ):
            size = 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if not (size <= MAX_ARCHIVE_BYTES):
                    raise ValueError(f"Cannot build Uni-Core: expanded archive exceeds {MAX_ARCHIVE_BYTES}-byte limit")
                target.write(chunk)
        source_root = scratch / root_name
        with tarfile.open(expanded, "r:") as archive:
            members = []
            names = set()
            for member in archive:
                if not (len(members) < MAX_MEMBERS):
                    raise ValueError(f"Cannot build Uni-Core: archive exceeds {MAX_MEMBERS} members")
                name = member.name.rstrip("/") if member.isdir() else member.name
                path = PurePosixPath(name)
                if (
                    not name
                    or path.is_absolute()
                    or "\\" in name
                    or any(part in ("", ".", "..") for part in name.split("/"))
                    or path.parts[0] != root_name
                    or not (member.isfile() or member.isdir())
                    or member.issparse()
                ):
                    raise ValueError(f"Cannot build Uni-Core: unsafe source member {member.name[:200]!r}")
                if name in names:
                    raise ValueError(f"Cannot build Uni-Core: duplicate source member {name[:200]!r}")
                names.add(name)
                members.append(member)
            if not any(member.name == root_name + "/setup.py" and member.isfile() for member in members):
                raise ValueError("Cannot build Uni-Core: pinned source has no regular setup.py")
            for member in members:
                destination = scratch / member.name
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as stream, destination.open("xb") as target:
                        shutil.copyfileobj(stream, target, length=1024 * 1024)
        # The pinned setup defaults to pure Python; torch is already installed from the lock.
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--no-build-isolation",
                "--no-cache-dir",
                "--disable-pip-version-check",
                str(source_root),
            ],
            check=True,
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("declaration", type=Path)
    args = parser.parse_args()
    build(args.archive, args.declaration)


if __name__ == "__main__":
    main()
