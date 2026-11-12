"""Acquire NMRNet's locked inputs with the shared wheelhouse publisher."""

from tools.container_guard import require_container

require_container()

import argparse
import fcntl
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from contextlib import contextmanager
from pathlib import Path

from pip._vendor.packaging.specifiers import Specifier
from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import InvalidWheelFilename, parse_wheel_filename
from pip._vendor.packaging.version import Version

from tools.wheelhouse import (
    WheelhouseNetworkError,
    core_assets,
    digest,
    prepare_core_assets,
    prepare_source,
    requirements,
    select_wheels,
    source_cache_file,
    source_identity,
    verified_source,
)


@contextmanager
def pool_lock(pool, exclusive):
    descriptor = os.open(pool, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield descriptor
    finally:
        os.close(descriptor)


def shared_asset_present(declaration, pool):
    """Check a single pinned release asset under the caller's pool lock."""
    assets = core_assets(declaration)
    if len(assets) != 1:
        raise ValueError("Cannot check shared release asset: expected one pinned URL.")
    _, _, name = assets[0]
    try:
        (pool / name).lstat()
    # silent-defaults: allow-exception-outcome -- An absent pinned asset reports not present; existing assets are verified before readiness.
    except FileNotFoundError:
        return False
    prepare_core_assets(declaration, pool, download=False)
    return True


def scientific_source_present(declaration, pool):
    identity = source_identity(declaration)
    source = source_cache_file(pool, identity)
    try:
        source.lstat()
    # silent-defaults: allow-exception-outcome -- An absent pinned source reports not present; existing source bytes are verified before readiness.
    except FileNotFoundError:
        return False
    verified_source(source, identity)
    return True


def refuse_conflicting_wheels(lock, pool):
    """Refuse a corrupt compatible cache entry when no valid alternative exists."""
    expected = requirements(lock)
    specifiers = {identity: Specifier(f"=={Version(identity[1])}") for identity in expected}
    tags = set(sys_tags())
    valid = set()
    conflicts = {}
    with os.scandir(pool) as entries:
        paths = [pool / entry.name for entry in entries if entry.name.endswith(".whl")]
    for path in paths:
        try:
            name, version, _, wheel_tags = parse_wheel_filename(path.name)
        except InvalidWheelFilename:
            continue
        if not tags.intersection(wheel_tags):
            continue
        identities = [
            identity
            for identity, specifier in specifiers.items()
            if identity[0] == name and specifier.contains(version, prereleases=True)
        ]
        if not identities:
            continue
        try:
            checksum = digest(path)
        except (OSError, ValueError):
            conflicts.update((identity, path.name) for identity in identities)
            continue
        for identity in identities:
            if checksum in expected[identity]:
                valid.add(identity)
            else:
                conflicts[identity] = path.name
    for identity, name in conflicts.items():
        if identity not in valid:
            raise ValueError(
                f"Cannot acquire wheels: cached wheel {name!r} is unreadable or differs from its lock; existing file preserved."
            )
    return set(expected) - valid


def missing_wheel_locks(locks, pool, descriptor):
    missing = []
    for lock in locks:
        try:
            select_wheels(lock, pool, lock_fd=descriptor)
        except subprocess.CalledProcessError as error:
            # Pip reports a caught KeyboardInterrupt as status 2.
            if error.returncode < 0 or error.returncode in (2, 130, 137, 143):
                raise SystemExit(128 - error.returncode if error.returncode < 0 else error.returncode) from error
            refuse_conflicting_wheels(lock, pool)
            missing.append(lock)
    return missing


def direct_wheels(paths, packages):
    """Use only wheel URLs already pinned in the input declarations."""
    result = []
    for path in paths:
        for raw_line in path.read_text().splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if " @ " not in line and not line.startswith(("https://", "http://")):
                continue
            url = line.split(" @ ", 1)[-1]
            parsed = urllib.parse.urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.query
                or not re.fullmatch(r"sha256=[0-9a-f]{64}", parsed.fragment)
            ):
                raise ValueError("Cannot acquire wheels: direct sources require HTTPS and a SHA-256 pin.")
            if not parsed.path.endswith(".whl"):
                continue
            try:
                name, _, _, _ = parse_wheel_filename(urllib.parse.unquote(Path(parsed.path).name))
            except InvalidWheelFilename as error:
                raise ValueError("Cannot acquire wheels: a pinned source has an invalid wheel filename.") from error
            if name in packages:
                result.append(url)
    return result


def run_shared(command, operation, **kwargs):
    try:
        subprocess.run(command, check=True, **kwargs)
    except subprocess.CalledProcessError as error:
        status = error.returncode if error.returncode > 0 else 128 - error.returncode
        sys.stderr.write(f"Cannot {operation}: the shared wheelhouse tool exited with status {status}.\n")
        raise SystemExit(status) from None


def acquire(locks, pool, assets, tool_assets, source=None, declarations=None, stream_assets=None):
    """Reuse the pool, then project missing locks and publish through the shared tool."""
    with tempfile.TemporaryDirectory(prefix="nmrnet-wheel-inputs-") as temporary:
        root = Path(temporary)
        frozen = []
        for index, original in enumerate(locks):
            lock = root / f"requirements-{index}.lock"
            shutil.copyfile(original, lock)
            frozen.append(lock)
        inputs = [assets, tool_assets]
        if declarations is not None:
            inputs.extend(declarations)
        declaration_stop = len(inputs)
        if source is not None:
            inputs.append(source)
        if stream_assets is not None:
            inputs.append(stream_assets)
        snapshots = []
        for index, original in enumerate(inputs):
            snapshot = root / f"declaration-{index}"
            shutil.copyfile(original, snapshot)
            snapshots.append(snapshot)
        assets, tool_assets = snapshots[:2]
        declarations = snapshots[2:declaration_stop]
        if source is not None:
            source = snapshots[declaration_stop]
        if stream_assets is not None:
            stream_assets = snapshots[-1]
        with pool_lock(pool, exclusive=False) as descriptor:
            missing = missing_wheel_locks(frozen, pool, descriptor)
        if source is not None or stream_assets is not None:
            with pool_lock(pool, exclusive=False):
                source_ready = source is None or scientific_source_present(source, pool)
                stream_ready = stream_assets is None or shared_asset_present(stream_assets, pool)
            if not source_ready or not stream_ready:
                with pool_lock(pool, exclusive=True) as descriptor:
                    missing = missing_wheel_locks(missing, pool, descriptor)
                    source_ready = source is None or scientific_source_present(source, pool)
                    stream_ready = stream_assets is None or shared_asset_present(stream_assets, pool)
                    if not stream_ready:
                        prepare_core_assets(stream_assets, pool, download=True)
                    if not source_ready:
                        prepare_source(source, pool)
        if not missing:
            sys.stdout.write("All locked wheels are already available in WHEELHOUSE; no download is needed.\n")
            sys.stdout.flush()
            return
        with pool_lock(pool, exclusive=True) as descriptor:
            still_missing = missing_wheel_locks(missing, pool, descriptor)
            if not still_missing:
                sys.stdout.write("All locked wheels are already available in WHEELHOUSE; no download is needed.\n")
                sys.stdout.flush()
                return
            jobs = [
                (lock, direct_wheels([assets, *declarations], {name for name, _version in requirements(lock)}))
                for lock in still_missing
            ]
            prepare_core_assets(tool_assets, pool, download=True)
        urls = [line.strip() for line in tool_assets.read_text().splitlines() if line.strip() and not line.startswith("#")]
        if len(urls) != 1:
            raise ValueError("Cannot acquire wheels: the shared tool declaration must contain one pinned asset.")
        tool = pool / Path(urllib.parse.urlsplit(urls[0]).path).name
        for index, (lock, sources) in enumerate(jobs):
            projected = root / f"pylock.{index}.toml"
            with projected.open("xb") as output:
                run_shared(
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        str(tool),
                        "locate",
                        "--lock",
                        str(lock),
                        "--index-url",
                        "https://pypi.org/simple",
                        "--pool",
                        str(pool),
                        *[argument for url in sources for argument in ("--source", url)],
                    ],
                    "locate locked wheels",
                    stdout=output,
                )
            run_shared(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    str(tool),
                    "acquire",
                    "--lock",
                    str(projected),
                    "--source-lock",
                    str(lock),
                    "--pool",
                    str(pool),
                ],
                "acquire locked wheels",
            )
            with pool_lock(pool, exclusive=False) as descriptor:
                select_wheels(lock, pool, lock_fd=descriptor)


def check(locks, pool, source=None, stream_assets=None):
    """Verify only the runtime closure and separately pinned scientific source."""
    with tempfile.TemporaryDirectory(prefix="nmrnet-wheel-check-") as temporary:
        root = Path(temporary)
        frozen = []
        for index, original in enumerate(locks):
            lock = root / f"requirements-{index}.lock"
            shutil.copyfile(original, lock)
            frozen.append(lock)
        with pool_lock(pool, exclusive=False) as descriptor:
            for lock in frozen:
                select_wheels(lock, pool, lock_fd=descriptor)
            if source is not None:
                declaration = source_identity(source)
                verified_source(source_cache_file(pool, declaration), declaration)
            if stream_assets is not None:
                prepare_core_assets(stream_assets, pool, download=False)
    sys.stdout.write(f"Verified locked wheels from {pool}\n")


def readiness(locks, pool, source=None, stream_assets=None):
    """Identify missing build inputs; a pip failure may have additional causes."""
    missing = False
    with tempfile.TemporaryDirectory(prefix="nmrnet-wheel-readiness-") as temporary:
        root = Path(temporary)
        frozen = []
        for index, original in enumerate(locks):
            lock = root / f"requirements-{index}.lock"
            shutil.copyfile(original, lock)
            frozen.append(lock)
        if source is not None:
            frozen_source = root / "source.json"
            shutil.copyfile(source, frozen_source)
            source = frozen_source
        if stream_assets is not None:
            frozen_assets = root / "stream-assets.in"
            shutil.copyfile(stream_assets, frozen_assets)
            stream_assets = frozen_assets
        with pool_lock(pool, exclusive=False) as descriptor:
            for lock in frozen:
                try:
                    select_wheels(lock, pool, lock_fd=descriptor)
                except subprocess.CalledProcessError as error:
                    if error.returncode != 1 or not refuse_conflicting_wheels(lock, pool):
                        raise
                    missing = True
            if source is not None:
                missing = not scientific_source_present(source, pool) or missing
            if stream_assets is not None:
                missing = not shared_asset_present(stream_assets, pool) or missing
    return not missing


def prepare_test_support(assets, pool, download, *, probe):
    """Acquire or check only the SDK source archive used by consumer tests."""
    lines = [line.strip() for line in assets.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    sources = [line for line in lines if urllib.parse.urlsplit(line.split("#", 1)[0]).path.endswith(".tar.gz")]
    if len(sources) != 1:
        raise ValueError("Cannot prepare provider test support: expected one pinned source archive.")
    with tempfile.TemporaryDirectory(prefix="nmrnet-test-source-") as temporary:
        declaration = Path(temporary) / "source.in"
        declaration.write_text(sources[0] + "\n")
        with pool_lock(pool, exclusive=False):
            present = shared_asset_present(declaration, pool)
        if not present:
            if probe:
                return False
            if not download:
                with pool_lock(pool, exclusive=False):
                    prepare_core_assets(declaration, pool, download=False)
            else:
                with pool_lock(pool, exclusive=True):
                    prepare_core_assets(declaration, pool, download=True)
    if probe:
        return True
    if not download:
        sys.stdout.write(f"Verified provider test support from {pool}\n")
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, action="append")
    parser.add_argument("--pool", type=Path, required=True)
    parser.add_argument("--assets", type=Path)
    parser.add_argument("--tool-assets", type=Path)
    parser.add_argument("--stream-assets", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--declaration", type=Path, action="append")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--support", action="store_true")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--tool-only", action="store_true")
    args = parser.parse_args()
    if args.support:
        if (
            args.assets is None
            or args.lock
            or args.source is not None
            or args.tool_assets is not None
            or args.stream_assets is not None
            or args.declaration
        ):
            parser.error("test support requires only --assets and --pool")
        if args.tool_only or (args.probe and args.check):
            parser.error("test support cannot combine probe with check or tool-only")
        result = prepare_test_support(args.assets, args.pool, download=not args.check, probe=args.probe)
        if args.probe:
            return 0 if result else 3
    elif args.tool_only:
        if (
            not args.probe
            or args.assets is None
            or args.lock
            or args.source
            or args.tool_assets
            or args.stream_assets
            or args.declaration
            or args.check
        ):
            parser.error("tool-only probe requires only --assets and --pool")
        with pool_lock(args.pool, exclusive=False):
            return 0 if shared_asset_present(args.assets, args.pool) else 3
    elif args.check:
        if args.probe:
            parser.error("check and probe cannot be combined")
        if not args.lock:
            parser.error("check requires at least one --lock")
        check(args.lock, args.pool, args.source, args.stream_assets)
    elif args.probe:
        if not args.lock or args.assets is None or args.tool_assets is None:
            parser.error("acquisition probe requires locks, release assets and the shared tool pin")
        return 0 if readiness(args.lock, args.pool, args.source, args.stream_assets) else 3
    else:
        if not args.lock or args.assets is None or args.tool_assets is None:
            parser.error("acquisition requires locks, release assets and the shared tool pin")
        acquire(args.lock, args.pool, args.assets, args.tool_assets, args.source, args.declaration, args.stream_assets)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WheelhouseNetworkError as error:
        sys.stderr.write(f"{error}\n")
        raise SystemExit(1) from None
