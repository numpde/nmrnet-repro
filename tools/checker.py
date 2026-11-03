"""Admit exact checker dependencies before acquisition or offline installation.

The dependency declaration owns versions and downloaded bytes. A complete
directory must contain precisely those regular files and a receipt bound to the
declaration; the receipt alone never proves that the current bytes are intact.
This follows the separate dependency-preparation boundary in Magnet's test base:
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/containers/test/Dockerfile.base
"""

from tools.container_guard import require_container

require_container()

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from tools.artifacts import fsync_directory, open_url, publish_directory, regular_file


def unique_fields(pairs):
    """Reject ambiguous JSON authority before any declaration is consumed."""
    document = {}
    for name, value in pairs:
        if name in document:
            raise ValueError("Cannot admit checker dependencies: duplicate JSON field")
        document[name] = value
    return document


def read_document(path):
    with regular_file(path, os.O_RDONLY) as stream:
        return json.load(stream, object_pairs_hook=unique_fields)


def file_identity(path):
    checksum = hashlib.sha256()
    size = 0
    with regular_file(path, os.O_RDONLY) as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            checksum.update(chunk)
    return {"size": size, "sha256": checksum.hexdigest()}


def admitted_declaration(path):
    """Read the closed Linux/amd64 dependency set, including transitive Debian inputs.

    Official Debian pool files and the official PyPI wheel host are the only
    acquisition authorities. Extra fields cannot silently add mirrors, resolver
    options or installation behavior. Fields and digest share one captured read,
    so replacement during artifact verification cannot rebind admitted inputs.
    NMRPeak applies the same closed-field rule:
    https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_weights.py#L47-L67
    """
    with regular_file(path, os.O_RDONLY) as stream:
        raw = stream.read()
    document = json.loads(raw, object_pairs_hook=unique_fields)
    declaration_sha256 = hashlib.sha256(raw).hexdigest()
    if not isinstance(document, dict) or set(document) != {"schema", "base", "platform", "packages"}:
        raise ValueError("Cannot admit checker dependencies: declaration fields must be schema, base, platform and packages")
    if document["schema"] != "nmrnet.checker-dependencies.v1" or document["platform"] != "linux/amd64":
        raise ValueError("Cannot admit checker dependencies: unsupported schema or platform")
    if (
        not isinstance(document["base"], str)
        or re.fullmatch(r"docker\.io/library/python:3\.12\.12-slim-bookworm@sha256:[0-9a-f]{64}", document["base"]) is None
    ):
        raise ValueError("Cannot admit checker dependencies: base must pin the Python 3.12.12 Bookworm image digest")
    packages = document["packages"]
    if not isinstance(packages, list) or not packages:
        raise ValueError("Cannot admit checker dependencies: packages must be a nonempty list")
    names = set()
    files = set()
    for package in packages:
        if not isinstance(package, dict) or set(package) != {"name", "version", "file", "url", "size", "sha256"}:
            raise ValueError(
                "Cannot admit checker dependencies: package fields must be name, version, file, url, size and sha256"
            )
        name, version, filename = package["name"], package["version"], package["file"]
        if not (
            isinstance(name, str)
            and re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name) is not None
            and isinstance(version, str)
            and re.fullmatch(r"[A-Za-z0-9.+:~_-]+", version) is not None
            and isinstance(filename, str)
            and name not in names
            and filename not in files
            and type(package["size"]) is int
            and package["size"] > 0
            and isinstance(package["sha256"], str)
            and re.fullmatch(r"[0-9a-f]{64}", package["sha256"]) is not None
            and isinstance(package["url"], str)
        ):
            raise ValueError("Cannot admit checker dependencies: invalid or duplicate package identity")
        url = urlsplit(package["url"])
        if url.scheme != "https" or url.query or url.fragment or unquote(url.path.rsplit("/", 1)[-1]) != filename:
            raise ValueError("Cannot admit checker dependencies: URL must name the exact HTTPS artifact")
        if name == "ruff":
            admitted = (
                url.netloc == "files.pythonhosted.org"
                and url.path.startswith("/packages/")
                and re.fullmatch(re.escape(f"ruff-{version}-") + r"py3-none-manylinux[^/]+_x86_64\.whl", filename)
            )
        else:
            admitted = (
                url.netloc == "deb.debian.org"
                and url.path.startswith(("/debian/pool/main/", "/debian-security/pool/updates/main/"))
                and filename in {f"{name}_{version.split(':', 1)[-1]}_{architecture}.deb" for architecture in ("amd64", "all")}
            )
        if not admitted:
            raise ValueError("Cannot admit checker dependencies: artifact host, package version or architecture differs")
        names.add(name)
        files.add(filename)
    if not {"git", "make", "ruff"} <= names:
        raise ValueError("Cannot admit checker dependencies: Git, Make and Ruff must all be declared")
    return document, declaration_sha256


def declaration(path):
    return admitted_declaration(path)[0]


def inventory_for_declaration(document, declaration_sha256, directory, wheelhouse=None):
    """Recompute identities against one already admitted declaration read."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Cannot verify checker dependencies: directory is missing or a symlink")
    expected = {package["file"]: {"size": package["size"], "sha256": package["sha256"]} for package in document["packages"]}
    actual = {entry.name for entry in directory.iterdir()} - {".complete"}
    wheel = next(package["file"] for package in document["packages"] if package["name"] == "ruff")
    required = set(expected) if wheelhouse is None else set(expected) - {wheel}
    if not required <= actual or actual - set(expected):
        raise ValueError(
            "Cannot verify checker dependencies: file inventory differs from declaration "
            f"({len(required - actual)} missing, {len(actual - set(expected))} unexpected); "
            "preserve this directory for inspection; move it aside before reacquiring at the same path"
        )
    for filename, identity in expected.items():
        artifact = Path(wheelhouse) / filename if wheelhouse is not None and filename == wheel else directory / filename
        if file_identity(artifact) != identity:
            raise ValueError(
                f"Cannot verify checker dependencies: artifact bytes differ from declaration for {filename!r}; "
                "preserve this directory for inspection; move it aside before reacquiring at the same path"
            )
        if wheelhouse is not None and filename == wheel and filename in actual and file_identity(directory / filename) != identity:
            raise ValueError("Cannot verify checker dependencies: retained private Ruff wheel differs from declaration")
    return {"declaration_sha256": declaration_sha256, "files": expected}


def inventory(path, directory):
    """Recompute exact membership and identities; reject links and extra inputs."""
    document, declaration_sha256 = admitted_declaration(path)
    return inventory_for_declaration(document, declaration_sha256, directory)


def check(path, directory, base, wheelhouse=None):
    document, declaration_sha256 = admitted_declaration(path)
    if document["base"] != base:
        raise ValueError("Cannot verify checker dependencies: declaration names another base image")
    expected = inventory_for_declaration(document, declaration_sha256, directory, wheelhouse)
    if read_document(Path(directory) / ".complete") != expected:
        raise ValueError("Cannot verify checker dependencies: completion receipt differs from admitted inputs")
    return expected


def checker_proxy():
    proxy = os.environ.get("HTTPS_PROXY")
    if not (proxy is not None and (match := re.fullmatch(r"http://127\.0\.0\.1:([0-9]+)", proxy)) is not None
            and 1 <= int(match[1]) <= 65535
            and all(os.environ.get(name) == proxy for name in ("HTTP_PROXY", "http_proxy", "https_proxy"))
            and all(os.environ.get(name) in (None, "") for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"))):
        raise ValueError("Cannot acquire checker dependencies: launcher-owned loopback proxy configuration is required")


def acquire_ruff(document, wheelhouse, tool_assets, stream_assets, legacy):
    with tempfile.TemporaryDirectory(prefix="checker-asset-pins-") as temporary:
        root = Path(temporary)
        frozen_tool = root / "acquirer.in"
        frozen_stream = root / "stream.in"
        shutil.copyfile(tool_assets, frozen_tool)
        shutil.copyfile(stream_assets, frozen_stream)
        _acquire_ruff(document, wheelhouse, frozen_tool, frozen_stream, legacy)


def _acquire_ruff(document, wheelhouse, tool_assets, stream_assets, legacy):
    """Reuse the declared wheel or publish it through the shared tool."""
    from tools.wheelhouse import prepare_core_assets
    from tools.wheelhouse_acquire import run_shared, shared_asset_present

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
        if not wheel_ready and (Path(legacy) / wheel["file"]).exists():
            raise ValueError("Cannot acquire checker Ruff wheel: a private cached wheel exists but the shared pool is missing it; preserve it and resolve the cache migration before retrying")
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
        lock.write_text('lock-version = "1.0"\ncreated-by = "nmrnet checker"\n'
                        '[[packages]]\nname = "ruff"\nversion = ' + json.dumps(wheel["version"]) + '\n'
                        '[packages.archive]\nurl = ' + json.dumps(wheel["url"]) + '\n'
                        '[packages.archive.hashes]\nsha256 = ' + json.dumps(wheel["sha256"]) + '\n')
        run_shared([sys.executable, "-I", "-S", str(tool), "acquire", "--lock", str(lock), "--pool", str(pool)],
                   "acquire the checker Ruff wheel")
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
        if (metadata.st_dev, metadata.st_ino) != (observed.st_dev, observed.st_ino) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise ValueError("Cannot resume checker dependencies: stage identity or private ownership differs")
        declared = {package["file"]: package for package in document["packages"] if package["name"] != "ruff"}
        actual = {item.name for item in stage.iterdir()} - {".complete"}
        if actual - declared.keys():
            raise ValueError("Cannot resume checker dependencies: stage contains unexpected files; existing evidence preserved")
        for name in actual:
            package = declared[name]
            if file_identity(stage / name) != {"size": package["size"], "sha256": package["sha256"]}:
                raise ValueError(f"Cannot resume checker dependencies: {name!r} differs from declaration; existing evidence preserved")
        if (stage / ".complete").exists() or (stage / ".complete").is_symlink():
            expected = {"declaration_sha256": declaration_sha256,
                        "files": {package["file"]: {"size": package["size"], "sha256": package["sha256"]}
                                  for package in document["packages"]}}
            if actual != declared.keys() or read_document(stage / ".complete") != expected:
                raise ValueError("Cannot resume checker dependencies: retained completion receipt differs; existing evidence preserved")
        yield stage
    finally:
        os.close(descriptor)


def download(path, output, base, wheelhouse=None, tool_assets=None, stream_assets=None, resume=None):
    """Publish only a complete verified dependency set; retain failed staging.

    The existing acquisition launcher supplies one loopback interface-bound
    proxy. Refusing bypass configuration prevents this worker from silently
    replacing that owner with ambient proxy or direct-network choices.
    """
    document, declaration_sha256 = admitted_declaration(path)
    if document["base"] != base:
        raise ValueError("Cannot acquire checker dependencies: declaration names another base image")
    if resume is not None:
        if wheelhouse is None:
            raise ValueError("Cannot resume checker dependencies without the shared wheelhouse")
        with resumed_stage(resume, output, document, declaration_sha256) as stage:
            return _download(document, declaration_sha256, output, base, wheelhouse, tool_assets, stream_assets, stage)
    return _download(document, declaration_sha256, output, base, wheelhouse, tool_assets, stream_assets, None)


def _download(document, declaration_sha256, output, base, wheelhouse, tool_assets, stream_assets, resume):
    output = Path(output)
    if wheelhouse is not None and (output.exists() or output.is_symlink()):
        legacy_wheel = next(package["file"] for package in document["packages"] if package["name"] == "ruff")
        verification_pool = None if (output / legacy_wheel).exists() else wheelhouse
        expected = inventory_for_declaration(document, declaration_sha256, output, verification_pool)
        if read_document(output / ".complete") != expected:
            raise ValueError("Cannot acquire checker dependencies: completion receipt differs from admitted declaration")
    if wheelhouse is not None:
        acquire_ruff(document, wheelhouse, tool_assets, stream_assets, output)
    if output.exists() or output.is_symlink():
        expected = inventory_for_declaration(document, declaration_sha256, output, wheelhouse)
        if read_document(output / ".complete") != expected:
            raise ValueError("Cannot acquire checker dependencies: completion receipt differs from admitted declaration")
        return
    checker_proxy()
    staged = resume if resume is not None else Path(tempfile.mkdtemp(prefix=".dependencies.pending-", dir=output.parent))
    if resume is None:
        descriptor = os.open(staged, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current = staged.lstat()
            held = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino) or any(staged.iterdir()):
                raise ValueError("Cannot acquire checker dependencies: the new stage changed before its lease; existing evidence preserved")
            return _finish_download(document, declaration_sha256, output, wheelhouse, staged, False)
        finally:
            os.close(descriptor)
    return _finish_download(document, declaration_sha256, output, wheelhouse, staged, True)


def _finish_download(document, declaration_sha256, output, wheelhouse, staged, resume):
    from tools.wheelhouse import network_request

    sys.stdout.write(f"Acquiring checker dependencies into {staged}; incomplete staging is retained on failure.\n")
    sys.stdout.flush()
    for package in document["packages"]:
        if wheelhouse is not None and package["name"] == "ruff":
            continue
        destination = staged / package["file"]
        if resume and destination.exists():
            continue
        count = 0
        name = f"checker dependency {package['file']}"
        with network_request(name, lambda: open_url(urllib.request.Request(package["url"]), timeout=60)) as response, destination.open("xb") as target:
            while chunk := network_request(name, lambda: response.read(1024 * 1024)):
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


def install(path, directory, base, wheelhouse=None):
    """Install verified local artifacts during the networkless checker build only.

    This owner uses the closed Debian closure rather than a live resolver or
    mirror options. Missing dependencies fail dpkg configuration and block image
    publication. The build caller grants this tool only captured dependencies.
    """
    document, declaration_sha256 = admitted_declaration(path)
    if document["base"] != base:
        raise ValueError("Cannot install checker dependencies: declaration names another base image")
    expected = inventory_for_declaration(document, declaration_sha256, directory, wheelhouse)
    if read_document(Path(directory) / ".complete") != expected:
        raise ValueError("Cannot install checker dependencies: completion receipt differs")
    debs = [str(Path(directory) / package["file"]) for package in document["packages"] if package["name"] != "ruff"]
    subprocess.run(["dpkg", "--install", *debs], check=True)
    ruff = next(package for package in document["packages"] if package["name"] == "ruff")
    with tempfile.TemporaryDirectory(prefix="checker-install-") as temporary:
        lock = Path(temporary) / "ruff.lock"
        lock.write_text(f"ruff=={ruff['version']} --hash=sha256:{ruff['sha256']}\n")
        subprocess.run(
            [
                "python3",
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                "--require-hashes",
                "--only-binary=:all:",
                "--no-cache-dir",
                "--disable-pip-version-check",
                "--find-links",
                str(wheelhouse if wheelhouse is not None else directory),
                "--requirement",
                str(lock),
            ],
            check=True,
        )
    for package in document["packages"]:
        if package["name"] == "ruff":
            result = subprocess.run(["ruff", "--version"], check=True, capture_output=True, text=True)
            observed = result.stdout.strip().removeprefix("ruff ")
        else:
            result = subprocess.run(
                ["dpkg-query", "--show", "--showformat=${Version}", package["name"]], check=True, capture_output=True, text=True
            )
            observed = result.stdout.strip()
        if observed != package["version"]:
            raise ValueError("Cannot install checker dependencies: installed version differs from declaration")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "check", "install"))
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--tool-assets", type=Path)
    parser.add_argument("--stream-assets", type=Path)
    parser.add_argument("--resume-stage", type=Path)
    args = parser.parse_args()
    if args.wheelhouse is None:
        parser.error("checker dependencies require --wheelhouse")
    if args.action != "download" and args.resume_stage is not None:
        parser.error("--resume-stage is only valid for checker acquisition")
    if args.action == "download":
        if args.tool_assets is None or args.stream_assets is None:
            parser.error("checker acquisition requires shared tool pins")
        from tools.wheelhouse import WheelhouseNetworkError

        try:
            download(args.declaration, args.directory, args.base, args.wheelhouse, args.tool_assets, args.stream_assets, args.resume_stage)
        except WheelhouseNetworkError as error:
            print(error, file=sys.stderr)
            raise SystemExit(1) from None
    elif args.action == "install":
        install(args.declaration, args.directory, args.base, args.wheelhouse)
    else:
        expected = check(args.declaration, args.directory, args.base, args.wheelhouse)
        if args.stream_assets is not None:
            from tools.wheelhouse import prepare_core_assets

            prepare_core_assets(args.stream_assets, args.wheelhouse, download=False)
        sys.stdout.write(expected["declaration_sha256"] + "\n")


if __name__ == "__main__":
    main()
