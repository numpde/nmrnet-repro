#!/usr/bin/env python3
"""Verify or regenerate the SDK proxy bootstrap from its pinned release."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / 'requirements/provider-core-release.json'
SNAPSHOT = ROOT / 'tools/bootstrap/provider_core'
MEMBERS = ('__init__.py', 'container_guard.py', 'bound_http_proxy.py')


def verify_source(name, data, record):
    expected = record['source_files'][f'src/provider_core/{name}']
    if len(data) != expected['size'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
        raise ValueError(f'SDK bootstrap file does not match release record: {name}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--wheel', type=Path, help='regenerate from this exact release wheel')
    args = parser.parse_args()
    record = json.loads(RECORD.read_text())
    if SNAPSHOT.parent.is_symlink() or SNAPSHOT.is_symlink() or (SNAPSHOT.exists() and not SNAPSHOT.is_dir()):
        raise ValueError('SDK bootstrap must be a directory without a symlink')
    if SNAPSHOT.exists() and {path.name for path in SNAPSHOT.iterdir()} - set(MEMBERS):
        raise ValueError('SDK bootstrap directory must contain exactly the release files')
    if args.wheel:
        wheel = args.wheel.read_bytes()
        expected = record['files'].get(args.wheel.name)
        if expected is None or hashlib.sha256(wheel).hexdigest() != expected:
            raise ValueError('SDK wheel does not match the pinned release record')
        with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
            names = archive.namelist()
            sources = {}
            for name in MEMBERS:
                member = f'provider_core/{name}'
                if names.count(member) != 1:
                    raise ValueError(f'SDK wheel must contain exactly one {member}')
                sources[name] = archive.read(member)
                verify_source(name, sources[name], record)
        SNAPSHOT.mkdir(parents=True, exist_ok=True)
        for name in sources:
            target = SNAPSHOT / name
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError(f'SDK bootstrap file must be regular and not a symlink: {name}')
        for name, source in sources.items():
            (SNAPSHOT / name).write_bytes(source)
    else:
        if {path.name for path in SNAPSHOT.iterdir()} != set(MEMBERS):
            raise ValueError('SDK bootstrap directory must contain exactly the release files')
        for name in MEMBERS:
            path = SNAPSHOT / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'SDK bootstrap file is not a regular file: {name}')
            verify_source(name, path.read_bytes(), record)


if __name__ == '__main__':
    main()
