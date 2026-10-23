"""Build small, valid wheels with independently declared software identities."""

from tools.container_guard import require_container

require_container()

import hashlib
import zipfile


def wheel(directory, name, version, tag="py3-none-any", payload=b"fixture wheel\n"):
    stem = f"{name.replace('-', '_')}-{version}"
    path = directory / f"{stem}-{tag}.whl"
    metadata = f"{stem}.dist-info"
    entries = {
        "fixture_payload.txt": payload,
        f"{metadata}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n".encode(),
        f"{metadata}/WHEEL": f"Wheel-Version: 1.0\nGenerator: repo-fixture\nRoot-Is-Purelib: true\nTag: {tag}\n".encode(),
        f"{metadata}/RECORD": b"",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for member, content in entries.items():
            info = zipfile.ZipInfo(member, (2020, 1, 1, 0, 0, 0))
            archive.writestr(info, content)
    return path


def locked(path, entries):
    path.write_text(
        "".join(
            f"{name}=={version} \\\n"
            + "".join(
                f"    --hash=sha256:{hashlib.sha256(candidate.read_bytes()).hexdigest()} \\\n" for candidate in candidates
            ).rstrip(" \\\n")
            + "\n"
            for name, version, candidates in entries
        )
    )
    return path
