"""Admit exact checker dependencies before acquisition or offline installation.

The dependency declaration owns versions and downloaded bytes. A complete
directory must contain precisely those regular files and a receipt bound to the
declaration; the receipt alone never proves that the current bytes are intact.
This follows the separate dependency-preparation boundary in Magnet's test base:
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/containers/test/Dockerfile.base
"""

from tools.container_guard import require_container

require_container()

import hashlib
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request
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
    if not isinstance(document["base"], str) or re.fullmatch(
        r"docker\.io/library/python:3\.12\.12-slim-bookworm@sha256:[0-9a-f]{64}", document["base"]
    ) is None:
        raise ValueError("Cannot admit checker dependencies: base must pin the Python 3.12.12 Bookworm image digest")
    packages = document["packages"]
    if not isinstance(packages, list) or not packages:
        raise ValueError("Cannot admit checker dependencies: packages must be a nonempty list")
    names = set()
    files = set()
    for package in packages:
        if not isinstance(package, dict) or set(package) != {"name", "version", "file", "url", "size", "sha256"}:
            raise ValueError("Cannot admit checker dependencies: package fields must be name, version, file, url, size and sha256")
        name, version, filename = package["name"], package["version"], package["file"]
        if (not isinstance(name, str) or re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", name) is None
                or not isinstance(version, str) or re.fullmatch(r"[A-Za-z0-9.+:~_-]+", version) is None
                or not isinstance(filename, str) or name in names or filename in files
                or type(package["size"]) is not int or package["size"] <= 0
                or not isinstance(package["sha256"], str)
                or re.fullmatch(r"[0-9a-f]{64}", package["sha256"]) is None
                or not isinstance(package["url"], str)):
            raise ValueError("Cannot admit checker dependencies: invalid or duplicate package identity")
        url = urlsplit(package["url"])
        if url.scheme != "https" or url.query or url.fragment or unquote(url.path.rsplit("/", 1)[-1]) != filename:
            raise ValueError("Cannot admit checker dependencies: URL must name the exact HTTPS artifact")
        if name == "ruff":
            admitted = (url.netloc == "files.pythonhosted.org" and url.path.startswith("/packages/")
                        and re.fullmatch(re.escape(f"ruff-{version}-") + r"py3-none-manylinux[^/]+_x86_64\.whl", filename))
        else:
            admitted = (url.netloc == "deb.debian.org"
                        and url.path.startswith(("/debian/pool/main/", "/debian-security/pool/updates/main/"))
                        and filename in {f"{name}_{version.split(':', 1)[-1]}_{architecture}.deb" for architecture in ("amd64", "all")})
        if not admitted:
            raise ValueError("Cannot admit checker dependencies: artifact host, package version or architecture differs")
        names.add(name)
        files.add(filename)
    if not {"git", "make", "ruff"} <= names:
        raise ValueError("Cannot admit checker dependencies: Git, Make and Ruff must all be declared")
    return document, declaration_sha256


def declaration(path):
    return admitted_declaration(path)[0]


def inventory_for_declaration(document, declaration_sha256, directory):
    """Recompute identities against one already admitted declaration read."""
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Cannot verify checker dependencies: directory is missing or a symlink")
    expected = {package["file"]: {"size": package["size"], "sha256": package["sha256"]}
                for package in document["packages"]}
    if {entry.name for entry in directory.iterdir()} - {".complete"} != set(expected):
        raise ValueError("Cannot verify checker dependencies: file inventory differs from declaration")
    for filename, identity in expected.items():
        if file_identity(directory / filename) != identity:
            raise ValueError("Cannot verify checker dependencies: artifact bytes differ from declaration")
    return {"declaration_sha256": declaration_sha256, "files": expected}


def inventory(path, directory):
    """Recompute exact membership and identities; reject links and extra inputs."""
    document, declaration_sha256 = admitted_declaration(path)
    return inventory_for_declaration(document, declaration_sha256, directory)


def check(path, directory):
    expected = inventory(path, directory)
    if read_document(Path(directory) / ".complete") != expected:
        raise ValueError("Cannot verify checker dependencies: completion receipt differs from admitted inputs")
    return expected


def download(path, output, base):
    """Publish only a complete verified dependency set; retain failed staging.

    The existing acquisition launcher supplies one loopback interface-bound
    proxy. Refusing bypass configuration prevents this worker from silently
    replacing that owner with ambient proxy or direct-network choices.
    """
    document, declaration_sha256 = admitted_declaration(path)
    if document["base"] != base:
        raise ValueError("Cannot acquire checker dependencies: declaration names another base image")
    output = Path(output)
    if output.exists() or output.is_symlink():
        expected = inventory_for_declaration(document, declaration_sha256, output)
        if read_document(output / ".complete") != expected:
            raise ValueError("Cannot acquire checker dependencies: completion receipt differs from admitted declaration")
        return
    proxy = os.environ.get("HTTPS_PROXY", "")
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]+)", proxy)
    if (match is None or not 1 <= int(match[1]) <= 65535
            or any(os.environ.get(name) != proxy for name in ("HTTP_PROXY", "http_proxy", "https_proxy"))
            or any(os.environ.get(name, "") for name in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"))):
        raise ValueError("Cannot acquire checker dependencies: launcher-owned loopback proxy configuration is required")
    staged = Path(tempfile.mkdtemp(prefix=".dependencies.pending-", dir=output.parent))
    print(f"Acquiring checker dependencies into {staged}; incomplete staging is retained on failure.", flush=True)
    for package in document["packages"]:
        destination = staged / package["file"]
        count = 0
        with open_url(urllib.request.Request(package["url"]), timeout=60) as response, destination.open("xb") as target:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > package["size"]:
                    raise ValueError("Cannot acquire checker dependencies: artifact exceeds its declared size")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if file_identity(destination) != {"size": package["size"], "sha256": package["sha256"]}:
            raise ValueError("Cannot acquire checker dependencies: artifact size or SHA-256 differs")
    receipt = inventory_for_declaration(document, declaration_sha256, staged)
    with (staged / ".complete").open("x") as stream:
        json.dump(receipt, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    fsync_directory(staged)
    publish_directory(staged, output)
    fsync_directory(output.parent)


def install(path, directory, base):
    """Install verified local artifacts during the networkless checker build only.

    This owner uses the closed Debian closure rather than a live resolver or
    mirror options. Missing dependencies fail dpkg configuration and block image
    publication. The build caller grants this tool only captured dependencies.
    """
    document, declaration_sha256 = admitted_declaration(path)
    if document["base"] != base:
        raise ValueError("Cannot install checker dependencies: declaration names another base image")
    expected = inventory_for_declaration(document, declaration_sha256, directory)
    if read_document(Path(directory) / ".complete") != expected:
        raise ValueError("Cannot install checker dependencies: completion receipt differs")
    debs = [str(Path(directory) / package["file"]) for package in document["packages"] if package["name"] != "ruff"]
    subprocess.run(["dpkg", "--install", *debs], check=True)
    ruff = next(package for package in document["packages"] if package["name"] == "ruff")
    with tempfile.TemporaryDirectory(prefix="checker-install-") as temporary:
        lock = Path(temporary) / "ruff.lock"
        lock.write_text(f"ruff=={ruff['version']} --hash=sha256:{ruff['sha256']}\n")
        subprocess.run(["python3", "-m", "pip", "install", "--no-index", "--no-deps", "--require-hashes",
                        "--only-binary=:all:", "--no-cache-dir", "--disable-pip-version-check",
                        "--find-links", str(directory), "--requirement", str(lock)], check=True)
    for package in document["packages"]:
        if package["name"] == "ruff":
            result = subprocess.run(["ruff", "--version"], check=True, capture_output=True, text=True)
            observed = result.stdout.strip().removeprefix("ruff ")
        else:
            result = subprocess.run(["dpkg-query", "--show", "--showformat=${Version}", package["name"]],
                                    check=True, capture_output=True, text=True)
            observed = result.stdout.strip()
        if observed != package["version"]:
            raise ValueError("Cannot install checker dependencies: installed version differs from declaration")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("download", "check", "install"))
    parser.add_argument("--declaration", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    if args.action == "download":
        download(args.declaration, args.directory, args.base)
    elif args.action == "install":
        install(args.declaration, args.directory, args.base)
    else:
        document, declaration_sha256 = admitted_declaration(args.declaration)
        if document["base"] != args.base:
            raise ValueError("Cannot verify checker dependencies: declaration names another base image")
        expected = inventory_for_declaration(document, declaration_sha256, args.directory)
        if read_document(args.directory / ".complete") != expected:
            raise ValueError("Cannot verify checker dependencies: completion receipt differs")
        print(declaration_sha256)


if __name__ == "__main__":
    main()
