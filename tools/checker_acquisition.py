"""Acquire checker inputs and verify the optional shared streaming tool."""

from tools.container_guard import require_container

require_container()

import fcntl
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.request
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from tools.artifacts import fsync_directory, open_url, publish_directory
from tools.checker import (
    admitted_declaration,
    check,
    file_identity,
    inventory_for_declaration,
    parse_arguments,
    private_inventory,
    read_document,
)
from tools.wheelhouse import WheelhouseNetworkError, network_request, prepare_core_assets
from tools.wheelhouse_acquire import run_shared, shared_asset_present


def checker_proxy():
    proxy = os.environ.get("HTTPS_PROXY")
    if not (
        proxy is not None
        and (match := re.fullmatch(r"http://127\.0\.0\.1:([0-9]+)", proxy)) is not None
        and 1 <= int(match[1]) <= 65535
        and all(os.environ.get(name) == proxy for name in ("HTTP_PROXY", "http_proxy", "https_proxy"))
        and all(os.environ.get(name) in (None, "") for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"))
    ):
        raise ValueError("Cannot acquire checker dependencies: launcher-owned loopback proxy configuration is required")


def acquire_ruff(document, wheelhouse, tool_assets, stream_assets):
    with tempfile.TemporaryDirectory(prefix="checker-asset-pins-") as temporary:
        root = Path(temporary)
        frozen_tool = root / "acquirer.in"
        frozen_stream = root / "stream.in"
        shutil.copyfile(tool_assets, frozen_tool)
        shutil.copyfile(stream_assets, frozen_stream)
        _acquire_ruff(document, wheelhouse, frozen_tool, frozen_stream)


def _acquire_ruff(document, wheelhouse, tool_assets, stream_assets):
    """Reuse the declared wheel or publish it through the shared tool."""
    wheel = next(package for package in document["packages"] if package["name"] == "ruff")
    pool = Path(wheelhouse)
    target = pool / wheel["file"]
    expected = {"size": wheel["size"], "sha256": wheel["sha256"]}
    descriptor = os.open(pool, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        try:
            target.lstat()
        except FileNotFoundError:
            wheel_ready = False
        else:
            if file_identity(target) != expected:
                raise ValueError("Cannot acquire checker Ruff wheel: cached bytes differ from declaration")
            wheel_ready = True
        stream_ready = shared_asset_present(stream_assets, pool)
        if wheel_ready and stream_ready:
            return
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            target.lstat()
        except FileNotFoundError:
            wheel_ready = False
        else:
            if file_identity(target) != expected:
                raise ValueError("Cannot acquire checker Ruff wheel: cached bytes differ from declaration")
            wheel_ready = True
        stream_ready = shared_asset_present(stream_assets, pool)
        acquirer_ready = wheel_ready or shared_asset_present(tool_assets, pool)
        if not stream_ready:
            checker_proxy()
            prepare_core_assets(stream_assets, pool, download=True)
        if wheel_ready:
            return
        if not acquirer_ready:
            checker_proxy()
            prepare_core_assets(tool_assets, pool, download=True)
    finally:
        os.close(descriptor)
    tool_line = next(line.strip() for line in Path(tool_assets).read_text().splitlines() if line.startswith("https://"))
    tool = pool / Path(urlsplit(tool_line).path).name
    with tempfile.TemporaryDirectory(prefix="checker-wheel-lock-") as temporary:
        lock = Path(temporary) / "pylock.checker.toml"
        lock.write_text(
            'lock-version = "1.0"\ncreated-by = "nmrnet checker"\n'
            '[[packages]]\nname = "ruff"\nversion = ' + json.dumps(wheel["version"]) + "\n"
            "[packages.archive]\nurl = " + json.dumps(wheel["url"]) + "\n"
            "[packages.archive.hashes]\nsha256 = " + json.dumps(wheel["sha256"]) + "\n"
        )
        run_shared(
            [sys.executable, "-I", "-S", str(tool), "acquire", "--lock", str(lock), "--pool", str(pool)],
            "acquire the checker Ruff wheel",
        )
    if file_identity(target) != expected:
        raise ValueError("Cannot acquire checker Ruff wheel: published bytes differ from declaration")


@contextmanager
def resumed_stage(stage, output, document, declaration_sha256):
    stage, output = Path(stage), Path(output)
    if stage.parent != output.parent or re.fullmatch(r"\.dependencies\.pending-[A-Za-z0-9_]+", stage.name) is None:
        raise ValueError("Cannot resume checker dependencies: select one retained direct-child stage")
    if output.exists() or output.is_symlink():
        raise ValueError("Cannot resume checker dependencies: the final directory already exists")
    descriptor = os.open(stage, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        metadata = os.fstat(descriptor)
        observed = stage.lstat()
        if (
            (metadata.st_dev, metadata.st_ino) != (observed.st_dev, observed.st_ino)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise ValueError("Cannot resume checker dependencies: stage identity or private ownership differs")
        declared = {package["file"]: package for package in document["packages"] if package["name"] != "ruff"}
        actual = {item.name for item in stage.iterdir()} - {".complete"}
        if actual - declared.keys():
            raise ValueError("Cannot resume checker dependencies: stage contains unexpected files; existing evidence preserved")
        for name in actual:
            package = declared[name]
            if file_identity(stage / name) != {"size": package["size"], "sha256": package["sha256"]}:
                raise ValueError(
                    f"Cannot resume checker dependencies: {name!r} differs from declaration; existing evidence preserved"
                )
        if (stage / ".complete").exists() or (stage / ".complete").is_symlink():
            expected = {
                "declaration_sha256": declaration_sha256,
                "files": {
                    package["file"]: {"size": package["size"], "sha256": package["sha256"]} for package in document["packages"]
                },
            }
            if actual != declared.keys() or read_document(stage / ".complete") != expected:
                raise ValueError(
                    "Cannot resume checker dependencies: retained completion receipt differs; existing evidence preserved"
                )
        yield stage
    finally:
        os.close(descriptor)


def download(path, output, base, wheelhouse, tool_assets=None, stream_assets=None, resume=None):
    """Publish only a complete verified dependency set; retain failed staging.

    The existing acquisition launcher supplies one loopback interface-bound
    proxy. Refusing bypass configuration prevents this worker from silently
    replacing that owner with ambient proxy or direct-network choices.
    """
    document, declaration_sha256 = admitted_declaration(path)
    if document["base"] != base:
        raise ValueError("Cannot acquire checker dependencies: declaration names another base image")
    if wheelhouse is None:
        raise ValueError("Cannot acquire checker dependencies without the shared wheelhouse")
    if resume is not None:
        with resumed_stage(resume, output, document, declaration_sha256) as stage:
            return _download(document, declaration_sha256, output, wheelhouse, tool_assets, stream_assets, stage)
    return _download(document, declaration_sha256, output, wheelhouse, tool_assets, stream_assets, None)


def _download(document, declaration_sha256, output, wheelhouse, tool_assets, stream_assets, resume):
    output = Path(output)
    if output.exists() or output.is_symlink():
        expected = private_inventory(document, declaration_sha256, output)
        if read_document(output / ".complete") != expected:
            raise ValueError("Cannot acquire checker dependencies: completion receipt differs from admitted declaration")
    acquire_ruff(document, wheelhouse, tool_assets, stream_assets)
    if output.exists() or output.is_symlink():
        expected = inventory_for_declaration(document, declaration_sha256, output, wheelhouse)
        if read_document(output / ".complete") != expected:
            raise ValueError("Cannot acquire checker dependencies: completion receipt differs from admitted declaration")
        return None
    checker_proxy()
    staged = resume if resume is not None else Path(tempfile.mkdtemp(prefix=".dependencies.pending-", dir=output.parent))
    if resume is None:
        descriptor = os.open(staged, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current = staged.lstat()
            held = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino) or any(staged.iterdir()):
                raise ValueError(
                    "Cannot acquire checker dependencies: the new stage changed before its lease; existing evidence preserved"
                )
            return _finish_download(document, declaration_sha256, output, wheelhouse, staged, False)
        finally:
            os.close(descriptor)
    return _finish_download(document, declaration_sha256, output, wheelhouse, staged, True)


def _finish_download(document, declaration_sha256, output, wheelhouse, staged, resume):
    sys.stdout.write(f"Acquiring checker dependencies into {staged}; incomplete staging is retained on failure.\n")
    sys.stdout.flush()
    for package in document["packages"]:
        if package["name"] == "ruff":
            continue
        destination = staged / package["file"]
        if resume and destination.exists():
            continue
        count = 0
        name = f"checker dependency {package['file']}"
        with (
            network_request(name, partial(open_url, urllib.request.Request(package["url"]), timeout=60)) as response,
            destination.open("xb") as target,
        ):
            while chunk := network_request(name, partial(response.read, 1024 * 1024)):
                count += len(chunk)
                if not (count <= package["size"]):
                    raise ValueError("Cannot acquire checker dependencies: artifact exceeds its declared size")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if file_identity(destination) != {"size": package["size"], "sha256": package["sha256"]}:
            raise ValueError("Cannot acquire checker dependencies: artifact size or SHA-256 differs")
    receipt = inventory_for_declaration(document, declaration_sha256, staged, wheelhouse)
    if not resume or not (staged / ".complete").exists():
        with (staged / ".complete").open("x") as stream:
            json.dump(receipt, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    elif read_document(staged / ".complete") != receipt:
        raise ValueError("Cannot resume checker dependencies: completion receipt changed; existing evidence preserved")
    fsync_directory(staged)
    publish_directory(staged, output)
    fsync_directory(output.parent)


def main():
    args = parse_arguments()
    if args.action == "download":
        try:
            download(
                args.declaration,
                args.directory,
                args.base,
                args.wheelhouse,
                args.tool_assets,
                args.stream_assets,
                args.resume_stage,
            )
        except WheelhouseNetworkError as error:
            sys.stderr.write(f"{error}\n")
            raise SystemExit(1) from None
    elif args.action == "check" and args.stream_assets is not None:
        expected = check(args.declaration, args.directory, args.base, args.wheelhouse)
        prepare_core_assets(args.stream_assets, args.wheelhouse, download=False)
        sys.stdout.write(expected["declaration_sha256"] + "\n")
    else:
        raise SystemExit("Cannot run checker acquisition: select download or check with --stream-assets.")


if __name__ == "__main__":
    main()
