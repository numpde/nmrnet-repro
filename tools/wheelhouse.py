"""Acquire hash-locked wheels and the pinned Uni-Core source without installing."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

from tools.artifacts import open_url, publish_directory, regular_file


SOURCE_NAME = "unicore-source.tar.gz"
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
    source = read_json(path)
    commit = source["commit"]
    if (source["name"] != "unicore" or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or source["url"] != f"https://codeload.github.com/dptech-corp/Uni-Core/tar.gz/{commit}"
            or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"])
            or type(source["size"]) is not int or source["size"] <= 0):
        raise ValueError("Cannot acquire Uni-Core source: invalid pinned source identity")
    return source


def inventory(lock, source_path, directory):
    expected = requirements(lock)
    source = source_identity(source_path)
    found = set()
    files = {}
    for path in sorted(directory.iterdir()):
        if path.name == ".complete":
            continue
        checksum = digest(path)
        if path.name == SOURCE_NAME:
            if path.stat().st_size != source["size"] or checksum != source["sha256"]:
                raise ValueError("Cannot verify wheelhouse: Uni-Core source size or hash differs from declaration")
        else:
            pieces = path.name.split("-")
            if not path.name.endswith(".whl") or len(pieces) < 5:
                raise ValueError(f"Cannot verify wheelhouse: unexpected file {path.name!r}")
            package = (re.sub(r"[-_.]+", "-", pieces[0]).lower(), pieces[1])
            if package not in expected or package in found:
                raise ValueError(f"Cannot verify wheelhouse: unlisted or duplicate wheel {path.name!r}")
            if checksum not in expected[package]:
                raise ValueError(f"Cannot verify wheelhouse: hash of {path.name!r} is not in the applied lock")
            found.add(package)
        files[path.name] = checksum
    if found != set(expected) or SOURCE_NAME not in files:
        raise ValueError("Cannot verify wheelhouse: required locked wheels or Uni-Core source are missing")
    return {"lock_sha256": digest(lock), "source_declaration_sha256": digest(source_path),
            "files": files}


def check(lock, source, output):
    if output.is_symlink() or not output.is_dir():
        raise ValueError("Cannot verify wheelhouse: output must be an existing directory without a symlink")
    receipt = read_json(output / ".complete")
    if receipt != inventory(lock, source, output):
        raise ValueError("Cannot verify wheelhouse: lock, source declaration or file manifest changed")


def download(lock, source_path, output):
    if output.exists() or output.is_symlink():
        check(lock, source_path, output)
        return
    requirements(lock)
    source = source_identity(source_path)
    stage = Path(tempfile.mkdtemp(prefix=f".{output.name}.pending-", dir=output.parent))
    print(f"Acquiring into {stage}; incomplete staging is preserved on failure.", flush=True)
    subprocess.run([
        sys.executable, "-m", "pip", "download", "--require-hashes", "--only-binary=:all:",
        "--no-deps", "--no-cache-dir", "--disable-pip-version-check",
        "--index-url", "https://pypi.org/simple", "--find-links", "https://download.pytorch.org/whl/cpu/torch/",
        "--dest", str(stage), "-r", str(lock),
    ], check=True)
    partial = stage / (SOURCE_NAME + ".part")
    checksum = hashlib.sha256()
    size = 0
    with open_url(urllib.request.Request(source["url"]), timeout=60) as response, partial.open("xb") as stream:
        while chunk := response.read(CHUNK_BYTES):
            size += len(chunk)
            if size > source["size"]:
                raise ValueError("Cannot acquire Uni-Core source: response exceeds declared size")
            stream.write(chunk)
            checksum.update(chunk)
    if size != source["size"] or checksum.hexdigest() != source["sha256"]:
        raise ValueError("Cannot acquire Uni-Core source: response size or hash differs from declaration")
    partial.rename(stage / SOURCE_NAME)
    receipt = inventory(lock, source_path, stage)
    (stage / ".complete").write_text(json.dumps(receipt, indent=2) + "\n")
    publish_directory(stage, output)
    print(f"Verified wheelhouse published at {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "check"))
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "download":
        download(args.lock, args.source, args.output)
    else:
        check(args.lock, args.source, args.output)
        print(f"Verified wheelhouse at {args.output}")


if __name__ == "__main__":
    main()
