"""Stage a resolved lock and advisory evidence; apply those exact bytes offline."""

from tools.container_guard import require_container

require_container()

import concurrent.futures
import hashlib
import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# These three inference inputs bind its staged and applied lock/report pair.
# repo-check: allow-module-constant
INPUTS = ("inference.in", "build.in", "unicore-source.json")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class LockProfile:
    """Bind admitted dependency intent to its own reviewed output pair."""

    inputs: tuple[str, ...]
    lock: str
    advisories: str


def lock_profile(name):
    """Select a fixed dependency closure; callers cannot supply output paths."""
    if name == "inference":
        return LockProfile(INPUTS, "inference.lock", "advisories.json")
    if name == "preparation":
        return LockProfile(("preparation.in",), "preparation.lock", "preparation-advisories.json")
    if name == "provider-core":
        return LockProfile(("provider-core.in",), "provider-core.lock", "provider-core-advisories.json")
    raise ValueError("Cannot select package profile: choose inference, preparation or provider-core.")


def input_hashes(profile):
    return {name: sha256((Path("/input") / name).read_bytes()) for name in profile.inputs}


def advisory(requirement):
    """Read exact-version advisory metadata with a bounded response size."""
    name, version = requirement
    # PyPI indexes the public Torch version; CPU local-version wheels share it.
    public_version = version.split("+", 1)[0]
    url = f"https://pypi.org/pypi/{name}/{public_version}/json"
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            data = response.read(8 * 1024 * 1024 + 1)
    except Exception as error:
        error.add_note(f"PyPI advisory request failed for {name}=={version}.")
        raise
    if not (len(data) <= 8 * 1024 * 1024):
        raise ValueError(f"Cannot audit {name}=={version}: metadata exceeds 8 MiB")
    try:
        metadata = json.loads(data)
        vulnerabilities = metadata["vulnerabilities"]
    except Exception as error:
        error.add_note(f"Invalid PyPI advisory metadata for {name}=={version}.")
        raise
    return {"name": name, "version": version, "url": url, "vulnerabilities": vulnerabilities}


def torch_wheel_declaration(intent):
    """Validate and return the one declared credential-free Torch wheel URL.

    Run this before invoking the resolver: its inherited output could echo a
    URL containing user information when a network request fails.
    """
    declarations = re.findall(r"^torch @ (https://\S+)$", intent.read_text(), flags=re.MULTILINE)
    if len(declarations) != 1:
        raise ValueError("Cannot stage lock: inference.in must declare exactly one hashed Torch CPU wheel URL")
    authority = declarations[0].removeprefix("https://").split("/", 1)[0]
    if "@" in authority:
        raise ValueError("Cannot stage lock: the declared Torch URL must not contain user information")
    declared = re.fullmatch(
        r"(?P<directory>https://[^\s#]+/)torch-(?P<version>[^/\s]+?)(?:%2B|\+)cpu-"
        r"cp312-cp312-manylinux_2_28_x86_64\.whl#sha256=(?P<digest>[0-9a-f]{64})",
        declarations[0],
    )
    if declared is None:
        raise ValueError("Cannot stage lock: the declared Torch URL must identify one hashed CPU CPython 3.12 Linux wheel")
    return declared


def normalize_torch_requirement(lock, declared):
    """Replace one declared CPU wheel URL with its pin and one existing hash.

    uv emits both the URL fragment and one artifact hash. Keeping that existing
    hash avoids manufacturing a duplicate during URL-to-pin conversion. Only
    the filename's equivalent %2B/+ spelling may vary from the declaration.
    """
    if not lock.endswith("\n") or "\r" in lock:
        raise ValueError("Cannot stage lock: resolver output must use LF-terminated text")
    lines = lock.splitlines(keepends=True)
    torch_lines = [index for index, line in enumerate(lines) if line.casefold().startswith(("torch @ ", "torch=="))]
    if len(torch_lines) != 1:
        raise ValueError("Cannot stage lock: resolver output must contain exactly one Torch requirement")
    position = torch_lines[0]
    url_line = (
        r"torch @ "
        + re.escape(declared["directory"])
        + r"torch-"
        + re.escape(declared["version"])
        + r"(?:%2B|\+)cpu-cp312-cp312-manylinux_2_28_x86_64\.whl#sha256="
        + declared["digest"]
        + r" \\\n"
    )
    if (
        re.fullmatch(url_line, lines[position]) is None
        or position + 1 == len(lines)
        or lines[position + 1] != f"    --hash=sha256:{declared['digest']}\n"
    ):
        raise ValueError("Cannot stage lock: generated Torch URL, ABI and sole artifact hash must match inference.in")
    for line in lines[position + 2 :]:
        if line.strip() and not line.startswith((" ", "\t")):
            break
        if line.strip() and not line.startswith("    #"):
            raise ValueError("Cannot stage lock: the generated Torch entry has additional or malformed artifact hashes")
    lines[position] = f"torch=={declared['version']}+cpu \\\n"
    return "".join(lines)


def stage(profile_name):
    profile = lock_profile(profile_name)
    output = Path("/output")
    before = input_hashes(profile)
    if profile_name == "inference":
        declared_torch = torch_wheel_declaration(Path("/input/inference.in"))
    raw = output / "resolved.raw"
    subprocess.run(
        [
            "uv",
            "pip",
            "compile",
            *("/input/" + name for name in profile.inputs if name.endswith(".in")),
            "--python-version",
            "3.12.12",
            "--python-platform",
            "x86_64-manylinux_2_28",
            "--generate-hashes",
            "--no-build",
            "--no-emit-index-url",
            "--no-emit-find-links",
            "--no-header",
            "--output-file",
            str(raw),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    lock = raw.read_bytes().decode("utf-8")
    if profile_name == "inference":
        lock = normalize_torch_requirement(lock, declared_torch)
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
    if input_hashes(profile) != before:
        raise ValueError("Dependency inputs changed during resolution; stage again")
    lock_bytes = lock.encode()
    (output / profile.lock).write_bytes(lock_bytes)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "PyPI exact-version advisory metadata; not a complete security audit",
        "inputs_sha256": before,
        "lock_sha256": sha256(lock_bytes),
        "packages": packages,
    }
    (output / profile.advisories).write_text(json.dumps(report, indent=2) + "\n")
    raw.unlink()
    count = sum(len(package["vulnerabilities"]) for package in packages)
    sys.stdout.write(
        f"Staged {len(packages)} exact package versions; {count} advisory entries. Review {profile.advisories} before applying.\n"
    )


def apply(profile_name):
    """Apply the reviewed pair, recovering predecessors after handled failure.

    Consumers read the two fixed requirements paths. Recovery preserves that
    layout; it does not make concurrent reads or process crashes atomic.
    """
    profile = lock_profile(profile_name)
    source = Path("/stage")
    lock = (source / profile.lock).read_bytes()
    report_bytes = (source / profile.advisories).read_bytes()
    report = json.loads(report_bytes)
    if sha256(lock) != report["lock_sha256"]:
        raise ValueError("Cannot apply lock: staged bytes differ from the advisory report")
    if input_hashes(profile) != report["inputs_sha256"]:
        raise ValueError("Cannot apply lock: dependency inputs changed after staging")
    outputs = ((profile.lock, lock), (profile.advisories, report_bytes))
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
            (work / ("candidate-" + name)).replace(output / name)
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
                    (work / ("previous-" + name)).replace(output / name)
                else:
                    (output / name).unlink(missing_ok=True)
            except OSError as recovery_error:
                uncertain = True
                error.add_note(
                    f"Cannot confirm predecessor restoration for {name}; retain apply evidence in {work}: {recovery_error}"
                )
        if not uncertain:
            try:
                shutil.rmtree(work)
            except OSError as cleanup_error:
                error.add_note(f"Predecessor outputs were restored; cannot remove apply evidence in {work}: {cleanup_error}")
            else:
                error.add_note("Previous output state was restored; the reviewed pair was not applied.")
        raise

    try:
        shutil.rmtree(work)
    except OSError as error:
        error.add_note(f"Both reviewed outputs were installed; cannot remove apply evidence in {work}.")
        raise
    sys.stdout.write(f"Applied reviewed lock {report['lock_sha256']} and advisory report offline\n")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] in ("stage", "apply"):
        profile_name = sys.argv[2]
        if sys.argv[1] == "stage":
            stage(profile_name)
        else:
            apply(profile_name)
    else:
        raise SystemExit("Usage: package_lock.py {stage|apply} {inference|preparation|provider-core}")
