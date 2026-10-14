"""Stage a resolved lock and advisory evidence; apply those exact bytes offline."""

from tools.container_guard import require_container

require_container()

import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request


INPUTS = ("inference.in", "build.in", "unicore-source.json")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def input_hashes():
    return {name: sha256((Path("/input") / name).read_bytes()) for name in INPUTS}


def advisory(requirement):
    name, version = requirement
    # PyPI indexes the public Torch version; CPU local-version wheels share it.
    public_version = version.split("+", 1)[0]
    url = f"https://pypi.org/pypi/{name}/{public_version}/json"
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(8 * 1024 * 1024 + 1)
    if len(data) > 8 * 1024 * 1024:
        raise ValueError(f"Cannot audit {name}=={version}: metadata exceeds 8 MiB")
    metadata = json.loads(data)
    return {"name": name, "version": version, "url": url,
            "vulnerabilities": metadata["vulnerabilities"]}


def stage():
    output = Path("/output")
    before = input_hashes()
    raw = output / "resolved.raw"
    subprocess.run([
        "uv", "pip", "compile", "/input/inference.in", "/input/build.in",
        "--python-version", "3.12.12", "--python-platform", "x86_64-manylinux_2_28",
        "--generate-hashes", "--no-build", "--no-emit-index-url",
        "--no-emit-find-links", "--no-header", "--output-file", str(raw),
    ], check=True, stdout=subprocess.DEVNULL)
    lock = raw.read_text()
    # Express the exact CPU wheel by version plus hash so later offline installs
    # use the local wheelhouse instead of fetching the direct URL again.
    lock = re.sub(
        r"^torch @ \S*/torch-([^/ ]+?)(?:%2B|\+)cpu-[^\s#]+#sha256=([0-9a-f]{64})(.*)$",
        lambda match: "torch==" + match[1] + "+cpu \\\n    --hash=sha256:" + match[2] + match[3],
        lock, flags=re.MULTILINE,
    )
    requirements = []
    for line in lock.splitlines():
        if not line.strip() or line.lstrip().startswith(("#", "--hash=")):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([^\s;]+) \\", line)
        if match is None:
            raise ValueError(f"Cannot audit lock: unsupported requirement line {line!r}")
        requirements.append(match.groups())
    if not requirements:
        raise ValueError("Cannot publish an empty dependency lock")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        packages = list(pool.map(advisory, requirements))
    if input_hashes() != before:
        raise ValueError("Dependency inputs changed during resolution; stage again")
    lock_bytes = lock.encode()
    (output / "inference.lock").write_bytes(lock_bytes)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "scope": "PyPI exact-version advisory metadata; not a complete security audit",
              "inputs_sha256": before, "lock_sha256": sha256(lock_bytes),
              "packages": packages}
    (output / "advisories.json").write_text(json.dumps(report, indent=2) + "\n")
    raw.unlink()
    count = sum(len(package["vulnerabilities"]) for package in packages)
    print(f"Staged {len(packages)} exact package versions; {count} advisory entries. Review advisories.json before applying.")


def apply():
    """Apply the reviewed pair, recovering predecessors after handled failure.

    Consumers read the two fixed requirements paths. Recovery preserves that
    layout; it does not make concurrent reads or process crashes atomic.
    """
    source = Path("/stage")
    lock = (source / "inference.lock").read_bytes()
    report_bytes = (source / "advisories.json").read_bytes()
    report = json.loads(report_bytes)
    if sha256(lock) != report["lock_sha256"]:
        raise ValueError("Cannot apply lock: staged bytes differ from the advisory report")
    if input_hashes() != report["inputs_sha256"]:
        raise ValueError("Cannot apply lock: dependency inputs changed after staging")
    outputs = (("inference.lock", lock), ("advisories.json", report_bytes))
    output = Path("/output")
    for name, _ in outputs:
        destination = output / name
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            raise ValueError(f"Cannot apply lock: {name} must be a regular file without a symlink")

    # SECS stages both locks before sequential publication. Preserve that
    # preparation boundary and additionally retain this pair's predecessors.
    # https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/make/packages.mk#L21-L71
    # NMRPeak atomically publishes one retained tree; our fixed-path consumers
    # need recovery because replacing their two files is not one tree rename.
    # https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/deployment/provider_deployment.py#L2221-L2273
    work = Path(tempfile.mkdtemp(prefix=".lock-apply.", dir=output))
    predecessors = set()
    try:
        for name, data in outputs:
            candidate = work / ("candidate-" + name)
            candidate.write_bytes(data)
            candidate.chmod(0o644)
            destination = output / name
            if destination.exists():
                predecessors.add(name)
                predecessor = work / ("previous-" + name)
                predecessor.write_bytes(destination.read_bytes())
                predecessor.chmod(stat.S_IMODE(destination.stat().st_mode))
    except BaseException as error:
        try:
            shutil.rmtree(work)
        except OSError as cleanup_error:
            error.add_note(f"Outputs were not replaced; cannot remove apply evidence in {work}: {cleanup_error}")
        raise

    try:
        for name, _ in outputs:
            os.replace(work / ("candidate-" + name), output / name)
    except BaseException as error:
        uncertain = False
        for name, _ in reversed(outputs):
            try:
                # Rename consumes its candidate, including when interruption
                # occurs after the effect but before Python resumes. Untouched
                # outputs need no restoration. Failed inspection is uncertainty.
                if (work / ("candidate-" + name)).exists():
                    continue
                if name in predecessors:
                    os.replace(work / ("previous-" + name), output / name)
                else:
                    (output / name).unlink(missing_ok=True)
            except OSError as recovery_error:
                uncertain = True
                error.add_note(f"Cannot confirm predecessor restoration for {name}; retain apply evidence in {work}: {recovery_error}")
        if not uncertain:
            try:
                shutil.rmtree(work)
            except OSError as cleanup_error:
                error.add_note(f"Predecessor outputs were restored; cannot remove apply evidence in {work}: {cleanup_error}")
        raise

    try:
        shutil.rmtree(work)
    except OSError as error:
        error.add_note(f"Both reviewed outputs were installed; cannot remove apply evidence in {work}.")
        raise
    print(f"Applied reviewed lock {report['lock_sha256']} and advisory report offline")


if __name__ == "__main__":
    if sys.argv[1:] == ["stage"]:
        stage()
    elif sys.argv[1:] == ["apply"]:
        apply()
    else:
        raise SystemExit("Usage: package_lock.py {stage|apply}")
