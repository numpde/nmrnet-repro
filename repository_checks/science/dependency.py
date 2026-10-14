"""Preserve strict NMRPeak lock assertions for NMRNet's .in intent.

Source: nmrpeak-repro f8055ab1065795fd46f8c1539e894cf9773f5495,
repository_checks/nmrpeak_lock.py. Torch's declared URL is intentionally
normalized to its exact CPU wheel version plus the same hash for offline
installation, matching tools/package_lock.py; other URL constraints stay exact.
https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_lock.py
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urldefrag, urlsplit

from repository_checks.science.source import regular_bytes

MAX_LOCK_BYTES = 16 * 1024 * 1024
PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;]+)")
REQUIREMENT = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(==[^ ]+| @ https://[^ ]+)")
HASH = re.compile(r"    --hash=sha256:([0-9a-f]{64})")
INPUT_NAMES = ("inference.in", "build.in", "unicore-source.json")


def normalized_name(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def direct_requirements(paths):
    requirements = {}
    index_owner = None
    for path in paths:
        for line in regular_bytes(path).decode("utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            if line.startswith("--"):
                if (line != "--index-url https://pypi.org/simple" or path.name != "inference.in"
                        or index_owner is not None):
                    raise ValueError(f"Cannot check dependency intent: only inference.in may declare the PyPI index once; rejected {path.name} option {line!r}")
                index_owner = path
                continue
            digest = None
            if " @ " in line:
                name, reference = line.split(" @ ", 1)
                url, fragment = urldefrag(reference)
                hashes = parse_qs(fragment, strict_parsing=True)
                if set(hashes) != {"sha256"} or len(hashes["sha256"]) != 1 or re.fullmatch(r"[0-9a-f]{64}", hashes["sha256"][0]) is None:
                    raise ValueError("Direct URL requirement lacks one SHA-256 digest")
                if urlsplit(url).scheme != "https":
                    raise ValueError("Direct dependency URL must use HTTPS")
                digest = hashes["sha256"][0]
                constraint = " @ " + url
                if normalized_name(name) == "torch":
                    filename = unquote(Path(urlsplit(url).path).name)
                    match = re.fullmatch(r"torch-([^/]+\+cpu)-cp312-cp312-manylinux_2_28_x86_64\.whl", filename)
                    if match is None:
                        raise ValueError("Direct Torch URL does not identify the declared CPU Python 3.12 platform wheel")
                    constraint = "==" + match[1]
            else:
                match = PIN.fullmatch(line)
                if match is None:
                    raise ValueError(f"Direct dependency intent is not an exact pin: {line}")
                name, version = match.groups()
                constraint = "==" + version
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name) is None:
                raise ValueError("Direct dependency name is invalid")
            key = normalized_name(name)
            if key in requirements:
                raise ValueError(f"Direct dependency is declared more than once: {name}")
            requirements[key] = (constraint, digest)
    if not requirements:
        raise ValueError("Direct dependency intent is empty")
    if index_owner is None:
        raise ValueError("Cannot check dependency intent: inference.in must declare --index-url https://pypi.org/simple exactly once")
    return requirements


def locked_requirements(raw):
    if not 0 < len(raw) <= MAX_LOCK_BYTES:
        raise ValueError("Lock size is outside its admitted nonempty byte budget")
    if not raw.endswith(b"\n") or b"\r" in raw:
        raise ValueError("Lock must use canonical LF-terminated text")
    lines = raw.decode("utf-8", errors="strict").splitlines()
    requirements = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line or line.startswith("    #"):
            index += 1
            continue
        if not line.endswith(" \\"):
            raise ValueError("Requirement line does not continue into artifact hashes")
        match = REQUIREMENT.fullmatch(line[:-2])
        if match is None:
            raise ValueError("Lock entry is not an exact pinned requirement")
        name, constraint = match.groups()
        key = normalized_name(name)
        if key in requirements:
            raise ValueError("Locked package is listed more than once")
        index += 1
        hashes = []
        continued = []
        while index < len(lines) and lines[index].startswith("    --hash="):
            trailing = lines[index].endswith(" \\")
            text = lines[index][:-2] if trailing else lines[index]
            match = HASH.fullmatch(text)
            if match is None:
                raise ValueError("Artifact hash is not canonical SHA-256")
            hashes.append(match[1])
            continued.append(trailing)
            index += 1
        if not hashes:
            raise ValueError("Locked requirement has no admitted SHA-256 artifact")
        if not all(continued[:-1]) or continued[-1]:
            raise ValueError("Hash continuations are not canonical")
        if hashes != sorted(set(hashes)):
            raise ValueError("Artifact hashes are duplicate or unsorted")
        requirements[key] = (constraint, tuple(hashes))
    if not requirements:
        raise ValueError("Locked requirement inventory is empty")
    if list(requirements) != sorted(requirements):
        raise ValueError("Requirements are not in canonical name order")
    return requirements


def check_lock(root, lock_path):
    locked = locked_requirements(regular_bytes(lock_path))
    direct = direct_requirements([root / "requirements" / name for name in ("inference.in", "build.in")])
    for name, (constraint, digest) in direct.items():
        if name not in locked:
            raise ValueError(f"Direct requirement is missing from the lock: {name}")
        actual, hashes = locked[name]
        matches = actual == constraint
        if constraint.startswith(" @ "):
            url, fragment = urldefrag(actual.removeprefix(" @ "))
            matches = actual.startswith(" @ ") and " @ " + url == constraint
            if fragment:
                matches = matches and parse_qs(fragment, strict_parsing=True) == {"sha256": [digest]}
        if not matches or (digest is not None and digest not in hashes):
            raise ValueError(f"Direct requirement version, source or artifact digest differs from intent: {name}")
    if any(constraint.startswith(" @ ") and name not in direct for name, (constraint, _) in locked.items()):
        raise ValueError("Transitive dependencies use unreviewed direct URLs")
    return locked


def check_advisory_binding(root):
    """The advisory report describes the same exact applied inputs and packages."""
    directory = root / "requirements"
    raw = regular_bytes(directory / "inference.lock")
    locked = locked_requirements(raw)
    report = json.loads(regular_bytes(directory / "advisories.json"))
    expected = {name: hashlib.sha256(regular_bytes(directory / name)).hexdigest() for name in INPUT_NAMES}
    if report["inputs_sha256"] != expected or report["lock_sha256"] != hashlib.sha256(raw).hexdigest():
        raise ValueError("Advisory evidence differs from the applied lock or direct inputs")
    pairs = [(normalized_name(package["name"]), "==" + package["version"]) for package in report["packages"]]
    if len(pairs) != len({name for name, _ in pairs}) or dict(pairs) != {name: value[0] for name, value in locked.items()}:
        raise ValueError("Advisory package inventory does not own every exact locked version once")
