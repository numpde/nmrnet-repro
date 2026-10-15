"""Authenticate declared, committed and materialized scientific source bytes.

Ported from nmrpeak-repro f8055ab1065795fd46f8c1539e894cf9773f5495,
repository_checks/nmrpeak_source.py and nmrpeak_image_inputs.py. The parent
gitlink, closure declaration, manifest, committed tree and live inventory must
agree. Git is part of this proof; source parsing cannot replace it.
https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_source.py
https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/repository_checks/nmrpeak_image_inputs.py
"""

from tools.container_guard import require_container

require_container()

import hashlib
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path, PurePosixPath

DECLARATION = Path("artifacts/nmrnet-source.paths")
MANIFEST = Path("artifacts/nmrnet-source.sha256")
UPSTREAM = Path("NMRNetplusplus")
IMAGE_INPUTS = Path("containers/inference/image-inputs.txt")


def regular_bytes(path):
    """Read a regular file without following its final symlink."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise ValueError(f"Required provenance input is not a readable regular file: {path}") from error
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"Provenance input is a special file: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def relative_path(value):
    path = PurePosixPath(value)
    if (
        not value
        or value == "."
        or path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or "\\" in value
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"Provenance declaration contains an invalid root or path: {value!r}")
    return path


def read_declaration(path):
    lines = regular_bytes(path).decode("ascii", errors="strict").splitlines()
    if not lines or not lines[0].startswith("source_revision "):
        raise ValueError("Source declaration has no revision")
    revision = lines[0].removeprefix("source_revision ")
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Source declaration revision is invalid")
    roots = tuple(lines[1:])
    if not roots or len(set(roots)) != len(roots):
        raise ValueError("Source declaration roots are empty or duplicated")
    paths = [relative_path(root) for root in roots]
    for index, path in enumerate(paths):
        if any(path in other.parents or other in path.parents for other in paths[index + 1 :]):
            raise ValueError("Source declaration roots overlap")
    return revision, roots


def read_manifest(path):
    hashes = {}
    for line in regular_bytes(path).decode("ascii", errors="strict").splitlines():
        digest, separator, name = line.partition("  ")
        if separator != "  " or re.fullmatch(r"[0-9a-f]{64}", digest) is None or name in hashes:
            raise ValueError("Source manifest is malformed or duplicated")
        relative_path(name)
        hashes[name] = digest
    if not hashes:
        raise ValueError("Source manifest is empty")
    return hashes


def git(repository, *arguments):
    binary = shutil.which("git")
    if binary is None:
        raise RuntimeError("Scientific repository checks require Git inside the maintained container; install no host substitute")
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        return subprocess.run(
            [binary, "-C", str(repository), *arguments], env=environment, check=True, capture_output=True
        ).stdout
    except subprocess.CalledProcessError as error:
        raise ValueError(f"Cannot inspect pinned Git source in {repository}; mount its Git object database read-only") from error


def committed_entries(repository, revision, roots):
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("Committed source revision must be one full Git object ID")
    entries = {}
    for raw in git(repository, "ls-tree", "-r", "-z", revision, "--", *roots).split(b"\0"):
        if not raw:
            continue
        metadata, raw_name = raw.split(b"\t", 1)
        mode, kind, object_id = metadata.decode("ascii").split(" ")
        if mode not in {"100644", "100755"} or kind != "blob":
            raise ValueError("Committed source contains a special file")
        name = raw_name.decode("utf-8", errors="strict")
        relative_path(name)
        entries[name] = (mode, object_id)
    if not entries:
        raise ValueError("Committed source inventory is empty")
    return entries


def live_paths(source, roots):
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Live source root is not a non-symlink directory")
    paths = []
    for root in roots:
        relative_path(root)
        ancestor = source
        for part in PurePosixPath(root).parts:
            ancestor /= part
            if ancestor.is_symlink():
                raise ValueError("Live source declaration traverses a symlink")
        candidate = source / root
        if candidate.is_symlink():
            raise ValueError("Live source contains a symlink")
        if candidate.is_file():
            paths.append(root)
            continue
        if not candidate.is_dir():
            raise ValueError(f"Live source root is missing or a special file: {root}")
        for directory, directories, files in os.walk(candidate):
            for name in directories:
                if (Path(directory) / name).is_symlink():
                    raise ValueError("Live source contains a symlink directory")
            for name in files:
                path = Path(directory) / name
                if path.is_symlink():
                    raise ValueError("Live source contains a symlink")
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError("Live source contains a special file")
                paths.append(path.relative_to(source).as_posix())
    return paths


def verify_materialized(source, declaration, manifest):
    _, roots = read_declaration(declaration)
    expected = read_manifest(manifest)
    if set(live_paths(source, roots)) != set(expected):
        raise ValueError("Live source inventory does not match the closure")
    for name, digest in expected.items():
        if hashlib.sha256(regular_bytes(source / name)).hexdigest() != digest:
            raise ValueError(f"Live source content drift: {name}")


def verify_source(source, declaration, manifest):
    revision, roots = read_declaration(declaration)
    expected = read_manifest(manifest)
    entries = committed_entries(source, revision, roots)
    if set(entries) != set(expected):
        raise ValueError("Source manifest does not match the committed closure")
    for name, (_, object_id) in entries.items():
        if hashlib.sha256(git(source, "cat-file", "blob", object_id)).hexdigest() != expected[name]:
            raise ValueError(f"Committed source content drift: {name}")
    if git(source, "rev-parse", "HEAD").decode("ascii").strip() != revision:
        raise ValueError("Submodule is not at the declared source revision")
    verify_materialized(source, declaration, manifest)


def verify_parent_gitlink(root):
    revision, roots = read_declaration(root / DECLARATION)
    if set(roots) != {"LICENSE", "uninmr"}:
        raise ValueError("NMRNet source closure must own exactly LICENSE and the complete uninmr runtime")
    raw = git(root, "ls-tree", "-z", "HEAD", "--", UPSTREAM.as_posix())
    entries = [entry for entry in raw.split(b"\0") if entry]
    if len(entries) != 1:
        raise ValueError("NMRNet upstream must be one committed parent gitlink")
    metadata, name = entries[0].split(b"\t", 1)
    mode, kind, object_id = metadata.decode("ascii").split(" ")
    if (mode, kind, object_id, name.decode()) != ("160000", "commit", revision, UPSTREAM.as_posix()):
        raise ValueError("NMRNet source declaration differs from its parent gitlink")
    verify_source(root / UPSTREAM, root / DECLARATION, root / MANIFEST)


def read_image_inputs(path):
    lines = regular_bytes(path).decode("utf-8", errors="strict").splitlines()
    if not lines or lines != sorted(set(lines)):
        raise ValueError("Image input declaration must be nonempty, sorted and unique")
    for name in lines:
        relative_path(name)
        if name == UPSTREAM.as_posix() or UPSTREAM.as_posix() + "/" in name:
            raise ValueError("Image inputs must admit upstream source through its closure manifest")
    required = {"containers/inference/Dockerfile", "containers/inference/Dockerfile.dockerignore"}
    if not required <= set(lines):
        raise ValueError("Image inputs omit the Dockerfile or its deny-by-default policy")
    return lines


def verify_image_inputs(root):
    """Prove the context declaration owns every committed inference build input."""
    declared = set(read_image_inputs(root / IMAGE_INPUTS))
    required = {
        "containers/inference/Dockerfile",
        "containers/inference/Dockerfile.dockerignore",
        "tools/container_guard.py",
        "tools/artifacts.py",
        "tools/wheelhouse.py",
        "tools/build_unicore.py",
        "requirements/inference.lock",
        "requirements/unicore-source.json",
        DECLARATION.as_posix(),
        MANIFEST.as_posix(),
    }
    if declared != required:
        raise ValueError("Inference image inputs do not own exactly the Docker policy, build tools, locks and source closure")
    revision = git(root, "rev-parse", "HEAD").decode("ascii").strip()
    entries = committed_entries(root, revision, tuple(sorted(declared)))
    if set(entries) != declared:
        raise ValueError("Inference image input declaration differs from its committed inventory")
    for name, (_, object_id) in entries.items():
        if regular_bytes(root / name) != git(root, "cat-file", "blob", object_id):
            raise ValueError(f"Inference image live input differs from committed source: {name}")
    verify_parent_gitlink(root)


def materialize_committed(repository, revision, roots, destination):
    """Build a test context from Git blobs, retaining committed executable modes."""
    if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("Committed image context destination must be an empty non-symlink directory")
    entries = committed_entries(repository, revision, roots)
    for name, (mode, object_id) in entries.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(git(repository, "cat-file", "blob", object_id))
        # silent-defaults: allow-contract -- committed_entries admits only Git regular-file modes 100644/100755; translate that closed set to 0644/0755; proof=repository_checks/test_source_closure.py::SourceClosureDetectionTests.test_context_has_exact_committed_inventory_bytes_modes_and_identity
        path.chmod(0o755 if mode == "100755" else 0o644)
    return context_digest(destination)


def context_digest(root):
    """Bind exactly the context paths, modes and bytes, without admitting links."""
    paths = live_paths(root, (path.name for path in root.iterdir()))
    if not paths:
        raise ValueError("Committed image context inventory is empty")
    digest = hashlib.sha256()
    for name in sorted(paths):
        raw = regular_bytes(root / name)
        label = name.encode("utf-8")
        mode = stat.S_IMODE((root / name).stat().st_mode)
        digest.update(len(label).to_bytes(4, "big"))
        digest.update(label)
        digest.update(mode.to_bytes(4, "big"))
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return "sha256:" + digest.hexdigest()
