"""The bootstrap must match the SDK release record before it can be used."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase, mock
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('bootstrap_snapshot', ROOT / 'tools/bootstrap_snapshot.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapSnapshotTests(TestCase):
    def test_offline_check_needs_no_selected_engine(self):
        result = subprocess.run(['make', 'packages/proxy-bootstrap/check'], cwd=ROOT,
                                env={**os.environ, 'DOCKER_CONTEXT': 'not-a-context'},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_checked_in_snapshot_matches_record(self):
        with mock.patch.object(sys, 'argv', ['bootstrap_snapshot']):
            bootstrap.main()

    def test_regenerate_and_detect_tampering(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / 'provider_core-test.whl'
            files = {name: name.encode() for name in bootstrap.MEMBERS}
            with zipfile.ZipFile(wheel, 'w') as archive:
                for name, data in files.items():
                    archive.writestr('provider_core/' + name, data)
            record = {'files': {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()},
                      'source_files': {f'src/provider_core/{name}': {'size': len(data),
                          'sha256': hashlib.sha256(data).hexdigest()} for name, data in files.items()}}
            record_path = root / 'release.json'
            record_path.write_text(json.dumps(record))
            snapshot = root / 'snapshot'
            with mock.patch.object(bootstrap, 'RECORD', record_path), mock.patch.object(bootstrap, 'SNAPSHOT', snapshot):
                with mock.patch.object(sys, 'argv', ['bootstrap_snapshot', '--wheel', str(wheel)]):
                    bootstrap.main()
                with mock.patch.object(sys, 'argv', ['bootstrap_snapshot']):
                    bootstrap.main()
                    (snapshot / 'unexpected.py').write_text('unexpected')
                    with self.assertRaisesRegex(ValueError, 'exactly the release files'):
                        bootstrap.main()
                    with mock.patch.object(sys, 'argv', ['bootstrap_snapshot', '--wheel', str(wheel)]):
                        with self.assertRaisesRegex(ValueError, 'exactly the release files'):
                            bootstrap.main()
                    (snapshot / 'unexpected.py').unlink()
                    (snapshot / 'bound_http_proxy.py').write_bytes(b'tampered')
                    with self.assertRaisesRegex(ValueError, 'does not match'):
                        bootstrap.main()
                    (snapshot / 'bound_http_proxy.py').unlink()
                    (snapshot / 'bound_http_proxy.py').symlink_to(wheel)
                    with mock.patch.object(sys, 'argv', ['bootstrap_snapshot', '--wheel', str(wheel)]):
                        with self.assertRaisesRegex(ValueError, 'not a symlink'):
                            bootstrap.main()
                wheel.write_bytes(b'tampered')
                with mock.patch.object(sys, 'argv', ['bootstrap_snapshot', '--wheel', str(wheel)]):
                    with self.assertRaisesRegex(ValueError, 'wheel does not match'):
                        bootstrap.main()
