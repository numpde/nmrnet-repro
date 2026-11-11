"""Verify locked artifacts and acquire pinned source or shared-tool assets."""

from tools.container_guard import require_container

require_container()

import argparse
import fcntl
import functools
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from tools.artifacts import fsync_directory, open_url, regular_file

# Prefix for one-file source-download temporaries in the shared pool.
# repo-check: allow-module-constant
SOURCE_TEMP_PREFIX = ".unicore-source."
# A fixed 1 MiB read buffer bounds streaming memory for hashes and downloads.
# repo-check: allow-module-constant
CHUNK_BYTES = 1024 * 1024


class WheelhouseNetworkError(OSError):
    """A direct wheelhouse fetch failed at its network boundary."""


def network_request(name, operation):
    """Report bounded transport evidence without exposing URLs or response text."""
    try:
        return operation()
    except (OSError, ValueError, http.client.HTTPException) as error:
        if isinstance(error, urllib.error.HTTPError):
            detail = f"the server returned HTTP {error.code}"
        else:
            reason = error.reason if isinstance(error, urllib.error.URLError) else error
            if isinstance(reason, socket.gaierror):
                detail = "name resolution failed"
            elif isinstance(reason, TimeoutError):
                detail = "the connection timed out"
            elif isinstance(reason, ssl.SSLError):
                detail = "the TLS connection failed"
            elif isinstance(reason, OSError) and (
                match := re.match(r"^Tunnel connection failed: ([0-9]{3})(?:\s|$)", str(reason))
            ):
                detail = f"the proxy tunnel returned HTTP {match[1]}"
            elif isinstance(reason, OSError) and isinstance(reason.errno, int):
                detail = f"the network failed with error number {reason.errno}"
            elif isinstance(reason, http.client.HTTPException):
                detail = "the HTTP connection failed"
            else:
                detail = "the network request failed"
        raise WheelhouseNetworkError(f"Cannot acquire {name}: {detail}.") from None


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


def acquire_source(source, partial):
    """Fetch the admitted Uni-Core archive to one private file in its pool."""
    checksum = hashlib.sha256()
    size = 0
    with (
        network_request("Uni-Core source", lambda: open_url(urllib.request.Request(source["url"]), timeout=60)) as response,
        partial.open("wb") as stream,
    ):
        while chunk := network_request("Uni-Core source", lambda: response.read(CHUNK_BYTES)):
            size += len(chunk)
            if not (size <= source["size"]):
                raise ValueError("Cannot acquire Uni-Core source: response exceeds declared size")
            stream.write(chunk)
            checksum.update(chunk)
    if size != source["size"] or checksum.hexdigest() != source["sha256"]:
        raise ValueError("Cannot acquire Uni-Core source: response size or hash differs from declaration")
    with regular_file(partial, os.O_RDONLY) as stream:
        os.fsync(stream.fileno())


def select_wheels(lock, pool, report_output=None, lock_fd=None):
    """Let pip select compatible, hash-locked files from an unrelated shared pool.

    A dry-run report avoids copying large wheels into the checker's small /tmp.
    No package is installed; --ignore-installed makes the pool prove every pin.
    https://pip.pypa.io/en/stable/cli/pip_install/#obtaining-information-about-what-was-installed
    https://pip.pypa.io/en/stable/reference/installation-report/
    """
    if not pool.is_dir():
        raise ValueError(f"Cannot select wheels: shared pool is not a directory: {pool}")
    with tempfile.TemporaryDirectory(prefix="wheel-selection-") as temporary:
        report = Path(temporary) / "report.json"
        subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--dry-run",
                "--ignore-installed",
                "--quiet",
                "--disable-pip-version-check",
                "--no-cache-dir",
                "--no-index",
                "--no-deps",
                "--require-hashes",
                "--only-binary=:all:",
                "--find-links",
                str(pool),
                "--report",
                str(report),
                "-r",
                str(lock),
            ],
            check=True,
            pass_fds=() if lock_fd is None else (lock_fd,),
        )
        identity = wheel_inputs_from_report(lock, pool, report)
        if report_output is not None:
            report_output.write(report.read_text())
        return identity


def wheel_inputs_from_report(lock, pool, report):
    """Project pip's actual selection or installation report into input identities."""
    selected = read_json(report)
    if selected["version"] != "1":
        raise ValueError("Cannot select wheels: pip returned an unsupported installation-report version.")
    files = {}
    for item in selected["install"]:
        location = urllib.parse.urlsplit(item["download_info"]["url"])
        candidate = Path(urllib.request.url2pathname(location.path))
        if (
            location.scheme != "file"
            or location.netloc
            or candidate.parent.resolve() != pool.resolve()
            or candidate.suffix != ".whl"
        ):
            raise ValueError("Cannot select wheels: pip selected a file outside the supplied flat pool.")
        # Pip owns lock resolution and source-file hash admission; callers use
        # this report to record the exact shared-pool inputs.
        files[candidate.name] = item["download_info"]["archive_info"]["hashes"]["sha256"]
    return {"lock_sha256": digest(lock), "source_declaration_sha256": None, "files": files}


def source_cache_file(directory, source):
    """Keep the separately verified non-wheel source in the shared root, keyed by pin."""
    return directory / f"Uni-Core-{source['commit']}.tar.gz"


def verified_source(path, source):
    with regular_file(path, os.O_RDONLY) as stream:
        if os.fstat(stream.fileno()).st_size != source["size"]:
            raise ValueError("Cannot verify Uni-Core source: cached size differs from its pinned declaration.")
        checksum = hashlib.sha256()
        while chunk := stream.read(CHUNK_BYTES):
            checksum.update(chunk)
    if checksum.hexdigest() != source["sha256"]:
        raise ValueError("Cannot verify Uni-Core source: cached SHA-256 differs from its pinned declaration.")


def publish_file(path, destination, checksum):
    """Add one complete file without replacing a peer's existing inode or bytes."""
    with regular_file(path, os.O_RDONLY) as stream:
        os.fchmod(stream.fileno(), 0o644)
        os.fsync(stream.fileno())
    try:
        os.link(path, destination, follow_symlinks=False)
    except FileExistsError as conflict:
        try:
            observed = digest(destination)
            if observed != checksum:
                raise ValueError(
                    f"Cannot reuse {destination.name!r}: expected SHA-256 {checksum}, found {observed}; the existing file is preserved."
                )
        except (OSError, ValueError) as invalid_winner:
            raise conflict from invalid_winner
    fsync_directory(destination.parent)


def prepare_source(source_path, directory):
    """Reuse or acquire the separately verified source in the same shared root."""
    source = source_identity(source_path)
    destination = source_cache_file(directory, source)
    if destination.exists() or destination.is_symlink():
        verified_source(destination, source)
        return
    descriptor, temporary = tempfile.mkstemp(prefix=SOURCE_TEMP_PREFIX, suffix=".part", dir=directory)
    os.close(descriptor)
    partial = Path(temporary)
    try:
        sys.stdout.write("Acquiring Uni-Core source directly into WHEELHOUSE.\n")
        sys.stdout.flush()
        acquire_source(source, partial)
        verified_source(partial, source)
        publish_file(partial, destination, source["sha256"])
    finally:
        partial.unlink(missing_ok=True)


def core_assets(inputs):
    """Read and validate pinned shared-owner release artifacts."""
    with regular_file(inputs, os.O_RDONLY) as stream:
        lines = stream.read().decode().splitlines()
    assets = []
    for line in lines:
        if not line or line.startswith("#"):
            continue
        url, checksum = line.rsplit("#sha256=", 1)
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or not parsed.path.startswith("/numpde/nmr-provider-tools/releases/download/")
            or re.fullmatch(r"[0-9a-f]{64}", checksum) is None
        ):
            raise ValueError(
                "Cannot acquire shared release artifact: require a pinned shared-owner GitHub release URL and SHA-256."
            )
        name = parsed.path.rsplit("/", 1)[1]
        if name in ("", ".", ".."):
            raise ValueError("Cannot acquire shared release artifact: the URL must name a file.")
        assets.append((url, checksum, name))
    return assets


def prepare_core_assets(inputs, pool, *, download):
    """Acquire pinned GitHub bytes directly, without invoking an index or a build."""
    for url, checksum, name in core_assets(inputs):
        destination = pool / name
        if destination.exists() or destination.is_symlink():
            if digest(destination) != checksum:
                raise ValueError(
                    f"Cannot reuse {name}: cached bytes differ from their GitHub release pin; existing file preserved."
                )
            continue
        if not download:
            raise ValueError(
                f"Cannot verify shared release artifact {name}: missing from the selected wheelhouse; run the matching acquisition target first."
            )
        descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", suffix=".part", dir=pool)
        os.close(descriptor)
        staged = Path(temporary)
        try:
            sys.stdout.write(f"Downloading shared release artifact: {name}\n")
            sys.stdout.flush()
            with (
                network_request(name, functools.partial(open_url, urllib.request.Request(url), timeout=60)) as response,
                staged.open("wb") as target,
            ):
                while chunk := network_request(name, lambda: response.read(CHUNK_BYTES)):
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            if digest(staged) != checksum:
                raise ValueError(f"Cannot publish {name}: downloaded bytes differ from their GitHub release pin.")
            publish_file(staged, destination, checksum)
        finally:
            staged.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "check", "report"))
    parser.add_argument("--profile", choices=("inference", "preparation", "provider-core", "shared-tool"))
    parser.add_argument("--lock", type=Path, action="append", default=[])
    parser.add_argument("--lock-sha256")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--pool", type=Path)
    args = parser.parse_args()
    if args.action == "download" and args.profile != "shared-tool":
        parser.error("locked wheel acquisition belongs to tools.wheelhouse_acquire; download accepts only --profile shared-tool")
    if args.action == "report":
        if args.profile is not None or args.source is not None or args.assets is not None or len(args.lock) != 1:
            parser.error("report requires one lock and no profile, source or assets")
        if args.lock_sha256 is None or not re.fullmatch(r"[0-9a-f]{64}", args.lock_sha256):
            parser.error("report requires the captured lock SHA-256")
    elif args.profile is None:
        parser.error(f"{args.action} requires --profile")
    elif args.lock_sha256 is not None:
        parser.error(f"{args.action} does not accept --lock-sha256")
    if args.profile == "shared-tool":
        if args.assets is None or args.lock or args.source is not None:
            parser.error("shared-tool requires only --assets and --pool")
    elif not args.lock:
        parser.error(f"{args.action} requires at least one --lock")
    if args.profile == "inference" and args.source is None:
        parser.error("inference requires --source for its pinned Uni-Core archive")
    if args.profile in {"preparation", "provider-core"} and args.source is not None:
        parser.error(f"{args.profile} accepts only locked wheels; omit --source")
    if args.pool is None:
        parser.error(f"{args.action} requires --pool for the selected shared directory")
    # A readonly bind does not prevent the host editing its source. Capture each
    # declaration once so pip, source verification and reported hashes consume
    # the same bytes throughout this operation.
    with tempfile.TemporaryDirectory(prefix="wheel-inputs-") as temporary:
        root = Path(temporary)
        locks = []
        for index, original in enumerate(args.lock):
            lock = root / f"requirements-{index}.lock"
            with regular_file(original, os.O_RDONLY) as stream, lock.open("xb") as target:
                shutil.copyfileobj(stream, target, CHUNK_BYTES)
            locks.append(lock)
        source_path = None
        if args.source is not None:
            source_path = root / "unicore-source.json"
            with regular_file(args.source, os.O_RDONLY) as stream, source_path.open("xb") as target:
                shutil.copyfileobj(stream, target, CHUNK_BYTES)
            source_identity(source_path)
        if args.action == "report":
            if digest(locks[0]) != args.lock_sha256:
                raise ValueError("Cannot select wheels: captured lock bytes differ from the expected SHA-256.")
            descriptor = os.open(args.pool, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH)
                select_wheels(locks[0], args.pool, report_output=sys.stdout, lock_fd=descriptor)
            finally:
                os.close(descriptor)
        elif args.action == "download":
            descriptor = os.open(args.pool, os.O_RDONLY | os.O_DIRECTORY)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                prepare_core_assets(args.assets, args.pool, download=True)
            finally:
                os.close(descriptor)
        else:
            if args.assets is not None:
                if args.profile == "shared-tool":
                    descriptor = os.open(args.pool, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_SH)
                        prepare_core_assets(args.assets, args.pool, download=False)
                    finally:
                        os.close(descriptor)
                else:
                    prepare_core_assets(args.assets, args.pool, download=False)
            files = set()
            for lock in locks:
                files.update(select_wheels(lock, args.pool)["files"])
            if source_path is not None:
                source = source_identity(source_path)
                verified_source(source_cache_file(args.pool, source), source)
            if args.profile == "shared-tool":
                sys.stdout.write(f"Verified shared wheelhouse tool from {args.pool}\n")
            else:
                sys.stdout.write(f"Verified {len(files)} locked wheels from {args.pool}\n")


if __name__ == "__main__":
    try:
        main()
    except WheelhouseNetworkError as error:
        sys.stderr.write(f"{error}\n")
        raise SystemExit(1) from None
