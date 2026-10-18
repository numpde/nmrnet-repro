"""Acquire hash-locked wheels and, for inference, pinned Uni-Core without installing."""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from tools.artifacts import fsync_directory, open_url, publish_directory, regular_file

# Acquisition and inventory share this one admitted Uni-Core archive filename.
# repo-check: allow-module-constant
SOURCE_NAME = "unicore-source.tar.gz"
# A fixed 1 MiB read buffer bounds streaming memory for hashes and downloads.
# repo-check: allow-module-constant
CHUNK_BYTES = 1024 * 1024


def digest(path):
    checksum = hashlib.sha256()
    with regular_file(path, os.O_RDONLY) as stream:
        while chunk := stream.read(CHUNK_BYTES):
            checksum.update(chunk)
    return checksum.hexdigest()


def read_json(path):
    with regular_file(path, os.O_RDONLY) as stream:
        return json.load(stream)


def requirements(lock):
    """Read the fixed name/version and allowed SHA-256 values emitted by our lock lane."""
    packages = {}
    current = None
    with regular_file(lock, os.O_RDONLY) as stream:
        for raw in stream:
            line = raw.decode().strip().removesuffix("\\").rstrip()
            if not line or line.startswith("#"):
                continue
            if match := re.fullmatch(r"--hash=sha256:([0-9a-f]{64})", line):
                if current is None:
                    raise ValueError("Cannot acquire wheels: hash precedes its locked package")
                packages[current].add(match[1])
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+)", line)
            if match is None:
                raise ValueError(f"Cannot acquire wheels: unsupported lock entry {line!r}")
            current = (re.sub(r"[-_.]+", "-", match[1]).lower(), match[2])
            if current in packages:
                raise ValueError(f"Cannot acquire wheels: duplicate locked package {current}")
            packages[current] = set()
    if not packages or not all(packages.values()):
        raise ValueError("Cannot acquire wheels: every locked package must have SHA-256 hashes")
    return packages


def source_identity(path):
    """Read the closed Uni-Core declaration before staging or acquisition.

    requirements/unicore-source.json owns these six fields; additions must not
    silently introduce ignored source authority. This follows the exact-field
    boundary in NMRPeak's read_declaration, using the local source identity.
    https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_weights.py#L47-L67
    """
    source = read_json(path)
    if not isinstance(source, dict) or set(source) != {
        "name",
        "version",
        "commit",
        "url",
        "size",
        "sha256",
    }:
        raise ValueError(
            "Cannot acquire Uni-Core source: declaration fields must be name, version, commit, url, size, and sha256"
        )
    commit = source["commit"]
    if not (
        source["name"] == "unicore"
        and re.fullmatch(r"[0-9a-f]{40}", commit)
        and source["url"] == f"https://codeload.github.com/dptech-corp/Uni-Core/tar.gz/{commit}"
        and re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
        and type(source["size"]) is int
        and source["size"] > 0
    ):
        raise ValueError("Cannot acquire Uni-Core source: invalid pinned source identity")
    return source


def inventory(lock, source_path, directory):
    """Verify exact membership; explicit None means a wheel-only preparation set."""
    expected = requirements(lock)
    source = None
    source_hash = None
    if source_path is not None:
        source = source_identity(source_path)
        source_hash = digest(source_path)
    found = set()
    files = {}
    for path in sorted(directory.iterdir()):
        if path.name == ".complete":
            continue
        checksum = digest(path)
        if source is not None and path.name == SOURCE_NAME:
            if path.stat().st_size != source["size"] or checksum != source["sha256"]:
                raise ValueError("Cannot verify wheelhouse: Uni-Core source size or hash differs from declaration")
        else:
            pieces = path.name.split("-")
            if not (path.name.endswith(".whl") and len(pieces) >= 5):
                raise ValueError(f"Cannot verify wheelhouse: unexpected file {path.name!r}")
            package = (re.sub(r"[-_.]+", "-", pieces[0]).lower(), pieces[1])
            if package not in expected or package in found:
                raise ValueError(f"Cannot verify wheelhouse: unlisted or duplicate wheel {path.name!r}")
            if checksum not in expected[package]:
                raise ValueError(f"Cannot verify wheelhouse: hash of {path.name!r} is not in the applied lock")
            found.add(package)
        files[path.name] = checksum
    missing_wheels = sorted(set(expected) - found)
    missing = [f"locked wheel {name}=={version}" for name, version in missing_wheels]
    if source is not None and SOURCE_NAME not in files:
        missing.append(f"Uni-Core source {SOURCE_NAME}")
    if missing:
        raise ValueError(f"Cannot verify wheelhouse: missing {', '.join(missing)}")
    return {"lock_sha256": digest(lock), "source_declaration_sha256": source_hash, "files": files}


def check(lock, source, output):
    if output.is_symlink() or not output.is_dir():
        raise ValueError("Cannot verify wheelhouse: output must be an existing directory without a symlink")
    receipt = read_json(output / ".complete")
    if receipt != inventory(lock, source, output):
        raise ValueError("Cannot verify wheelhouse: lock, source declaration or file manifest changed")


def acquire_source(source, stage):
    """Fetch the admitted Uni-Core archive with its declared byte and hash bounds."""
    partial = stage / (SOURCE_NAME + ".part")
    checksum = hashlib.sha256()
    size = 0
    with open_url(urllib.request.Request(source["url"]), timeout=60) as response, partial.open("xb") as stream:
        while chunk := response.read(CHUNK_BYTES):
            size += len(chunk)
            if not (size <= source["size"]):
                raise ValueError("Cannot acquire Uni-Core source: response exceeds declared size")
            stream.write(chunk)
            checksum.update(chunk)
    if size != source["size"] or checksum.hexdigest() != source["sha256"]:
        raise ValueError("Cannot acquire Uni-Core source: response size or hash differs from declaration")
    partial.rename(stage / SOURCE_NAME)


def download(lock, source_path, output):
    """Reuse verified bytes or publish a complete set; None explicitly excludes source."""
    if output.exists() or output.is_symlink():
        check(lock, source_path, output)
        return
    requirements(lock)
    source = None
    indexes = []
    if source_path is not None:
        source = source_identity(source_path)
        # Only the inference closure needs the separately published CPU Torch
        # wheels. Preparation has no reason to contact this additional index.
        indexes = ["--find-links", "https://download.pytorch.org/whl/cpu/torch/"]
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.pending-", dir=output.parent))
    sys.stdout.write(f"Acquiring into {stage}; incomplete staging is preserved on failure.\n")
    sys.stdout.flush()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-deps",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--index-url",
            "https://pypi.org/simple",
            *indexes,
            "--dest",
            str(stage),
            "-r",
            str(lock),
        ],
        check=True,
    )
    if source is not None:
        acquire_source(source, stage)
    receipt = inventory(lock, source_path, stage)
    (stage / ".complete").write_text(json.dumps(receipt, indent=2) + "\n")
    # Follow tools/artifacts.py:extract's durability order: persist verified files
    # and their directory before publication, then persist the renamed parent entry.
    for name in (*receipt["files"], ".complete"):
        with regular_file(stage / name, os.O_RDONLY) as stream:
            os.fsync(stream.fileno())
    fsync_directory(stage)
    publish_directory(stage, output)
    fsync_directory(output.parent)
    sys.stdout.write(f"Verified wheelhouse published at {output}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "check"))
    parser.add_argument("--profile", choices=("inference", "preparation"), required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.profile == "inference" and args.source is None:
        parser.error("inference requires --source for its pinned Uni-Core archive")
    if args.profile == "preparation" and args.source is not None:
        parser.error("preparation accepts only locked wheels; omit --source")
    if args.action == "download":
        download(args.lock, args.source, args.output)
    else:
        check(args.lock, args.source, args.output)
        sys.stdout.write(f"Verified wheelhouse at {args.output}\n")


if __name__ == "__main__":
    main()
