"""Acquire hash-locked wheels and, for inference, pinned Uni-Core without installing."""

from tools.container_guard import require_container

require_container()

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from tools.artifacts import fsync_directory, open_url, regular_file

# Private image captures use this stable name; shared source files are commit-keyed.
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


def select_wheels(lock, pool):
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
        )
        return wheel_inputs_from_report(lock, pool, report)


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
        # Pip owns lock resolution and source-file hash admission. Capture later
        # compares its copied bytes with these hashes and the original lock.
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


def capture(lock, source_path, pool, destination):
    """Freeze only the selected files; the shared pool has no whole-pool receipt."""
    if not destination.is_dir() or destination.is_symlink() or any(destination.iterdir()):
        raise ValueError("Cannot capture wheels: destination must be a private empty directory.")
    selected = select_wheels(lock, pool)
    for name in selected["files"]:
        with regular_file(pool / name, os.O_RDONLY) as source, (destination / name).open("xb") as target:
            shutil.copyfileobj(source, target, CHUNK_BYTES)
            os.fchmod(target.fileno(), 0o644)
    if source_path is not None:
        source = source_identity(source_path)
        archive = source_cache_file(pool, source)
        verified_source(archive, source)
        with regular_file(archive, os.O_RDONLY) as stream, (destination / SOURCE_NAME).open("xb") as target:
            shutil.copyfileobj(stream, target, CHUNK_BYTES)
            os.fchmod(target.fileno(), 0o644)
        selected["source_declaration_sha256"] = digest(source_path)
        selected["files"][SOURCE_NAME] = source["sha256"]
    copied = {name: digest(destination / name) for name in selected["files"]}
    if copied != selected["files"]:
        raise ValueError("Cannot capture wheels: selected bytes or lock inputs changed while copying.")
    return selected


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


def populate_pool(lock, pool, *, inference):
    """Download a lock privately and publish individual verified wheels additively.

    SECS supplies the pip invocation, not its replace-whole-directory policy:
    https://github.com/numpde/secs-repro/blob/7632f35f22ac20c70ff7c112531f4f32b6d2bd7b/make/packages.mk#L91-L104
    https://pip.pypa.io/en/stable/cli/pip_download/

    pip 24.2 resolves the complete requirements input before it saves any linked
    requirements. Its public HTTP cache can retain successful responses across a
    failed whole-lock download, so keep that cache in the caller-selected pool
    and let pip reuse it on retry:
    https://github.com/pypa/pip/blob/24.2/src/pip/_internal/commands/download.py#L132-L141
    https://pip.pypa.io/en/stable/topics/caching/
    """
    requirements(lock)
    if not pool.is_dir():
        raise ValueError("Cannot populate wheels: shared pool must already be a directory.")
    # Same-filesystem staging permits no-replace link publication. Cleanup is
    # success-only: a failure keeps its downloaded evidence and original cause.
    stage = Path(tempfile.mkdtemp(prefix=".nmrnet-wheel-download-", dir=pool))
    sys.stdout.write(
        f"Downloading locked wheels. Temporary directory under your selected WHEELHOUSE: {stage.name}/ (kept on failure).\n"
    )
    sys.stdout.flush()
    indexes = ["--find-links", "https://download.pytorch.org/whl/cpu/torch/"] if inference else []
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "pip",
            "--isolated",
            "download",
            "--require-hashes",
            "--only-binary=:all:",
            "--no-deps",
            "--cache-dir",
            str(pool / ".pip-cache"),
            "--disable-pip-version-check",
            "--index-url",
            "https://pypi.org/simple",
            "--find-links",
            str(pool),
            *indexes,
            "--dest",
            str(stage),
            "-r",
            str(lock),
        ],
        check=True,
    )
    selected = select_wheels(lock, stage)
    for name, checksum in selected["files"].items():
        publish_file(stage / name, pool / name, checksum)
    shutil.rmtree(stage)
    return selected


def prepare_source(source_path, directory):
    """Reuse or acquire the separately verified source in the same shared root."""
    source = source_identity(source_path)
    destination = source_cache_file(directory, source)
    if destination.exists() or destination.is_symlink():
        verified_source(destination, source)
        return
    stage = Path(tempfile.mkdtemp(prefix=".unicore-source-", dir=directory))
    sys.stdout.write(
        f"Downloading Uni-Core source. Temporary directory under your selected WHEELHOUSE: {stage.name}/ (kept on failure).\n"
    )
    sys.stdout.flush()
    acquire_source(source, stage)
    verified_source(stage / SOURCE_NAME, source)
    publish_file(stage / SOURCE_NAME, destination, source["sha256"])
    shutil.rmtree(stage)


def prepare_core_assets(inputs, pool, *, download):
    """Acquire pinned GitHub bytes directly, without invoking an index or a build."""
    with regular_file(inputs, os.O_RDONLY) as stream:
        lines = stream.read().decode().splitlines()
    for line in lines:
        if not line or line.startswith("#"):
            continue
        url, checksum = line.rsplit("#sha256=", 1)
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "github.com"
                or not parsed.path.startswith("/numpde/nmr-provider-tools/releases/download/")
                or re.fullmatch(r"[0-9a-f]{64}", checksum) is None):
            raise ValueError("Cannot acquire provider-core: require a pinned shared-owner GitHub release URL and SHA-256.")
        name = parsed.path.rsplit("/", 1)[1]
        destination = pool / name
        if destination.exists() or destination.is_symlink():
            if digest(destination) != checksum:
                raise ValueError(f"Cannot reuse {name}: cached bytes differ from their GitHub release pin; existing file preserved.")
            continue
        if not download:
            raise ValueError(f"Cannot verify shared release artifact {name}: missing from the selected wheelhouse; acquire provider-core artifacts first.")
        with tempfile.TemporaryDirectory(prefix=".nmrnet-core-download-", dir=pool) as temporary:
            staged = Path(temporary) / name
            print(f"Downloading shared release artifact: {name}", flush=True)
            with open_url(urllib.request.Request(url), timeout=60) as response, staged.open("xb") as target:
                shutil.copyfileobj(response, target, CHUNK_BYTES)
            if digest(staged) != checksum:
                raise ValueError(f"Cannot publish {name}: downloaded bytes differ from their GitHub release pin.")
            publish_file(staged, destination, checksum)


def core_dependencies(lock, destination):
    """Let PyPI supply third-party dependencies only; the private wheel has its own origin."""
    packages = requirements(lock)
    if sum(name == "provider-core" for name, _version in packages) != 1:
        raise ValueError("Cannot acquire shared dependencies: the installation lock must identify provider-core once.")
    with destination.open("x") as stream:
        for (name, version), hashes in sorted(packages.items()):
            if name != "provider-core":
                stream.write(" \\\n    ".join([f"{name}=={version}", *(f"--hash=sha256:{value}" for value in sorted(hashes))]) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "check", "capture"))
    parser.add_argument("--profile", choices=("inference", "preparation", "provider-core"), required=True)
    parser.add_argument("--lock", type=Path, action="append", required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--pool", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.profile == "inference" and args.source is None:
        parser.error("inference requires --source for its pinned Uni-Core archive")
    if args.profile in {"preparation", "provider-core"} and args.source is not None:
        parser.error(f"{args.profile} accepts only locked wheels; omit --source")
    if args.pool is None:
        parser.error(f"{args.action} requires --pool for the selected shared directory")
    if args.action == "capture" and args.output is None:
        parser.error(f"{args.action} requires --output for its private capture")
    if args.action == "capture" and len(args.lock) != 1:
        parser.error(f"{args.action} requires one lock for its private dependency capture")
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
        if args.action == "download":
            if args.profile == "provider-core":
                if args.assets is None or len(locks) != 1:
                    parser.error("provider-core acquisition requires --assets and one released installation lock")
                prepare_core_assets(args.assets, args.pool, download=True)
                third_party = root / "third-party.lock"
                core_dependencies(locks[0], third_party)
                populate_pool(third_party, args.pool, inference=False)
                select_wheels(locks[0], args.pool)
                return
            for lock in locks:
                populate_pool(lock, args.pool, inference=args.profile == "inference")
            if source_path is not None:
                prepare_source(source_path, args.pool)
        elif args.action == "capture":
            capture(locks[0], source_path, args.pool, args.output)
        else:
            if args.profile == "provider-core" and args.assets is not None:
                prepare_core_assets(args.assets, args.pool, download=False)
            files = set()
            for lock in locks:
                files.update(select_wheels(lock, args.pool)["files"])
            if source_path is not None:
                source = source_identity(source_path)
                verified_source(source_cache_file(args.pool, source), source)
            sys.stdout.write(f"Verified {len(files)} locked wheels from {args.pool}\n")


if __name__ == "__main__":
    main()
