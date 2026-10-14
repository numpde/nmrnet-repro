"""Exercise the maintained launcher with an empty contained proof directory.

Port: https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_unittest_runner.py#L10
Magnet's runner rejects zero tests with status 2. The fake engine executes the
Python argv actually selected by tools/container.sh, without a socket or mounts.
"""

from tools.container_guard import require_container

require_container()

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from repository_checks.fixtures.containers.fixture import LauncherFixture
from repository_checks.quality.sources import ROOT

FIXTURES = ROOT / 'repository_checks/fixtures/containers'


class UnittestRunnerTests(unittest.TestCase):
    def test_empty_discovery_is_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = LauncherFixture(root)
            empty = root / 'empty'
            empty.mkdir()
            additions = {
                'NMRNET_FAKE_EMPTY_DISCOVERY': '1', 'NMRNET_FAKE_EMPTY_DIRECTORY': str(empty)}
            for lane in ('test', 'test/repository'):
                with self.subTest(lane=lane):
                    result = fixture.run('container.sh', lane, additions=additions)
                    self.assertEqual(2, result.returncode, msg='An empty maintained proof lane must fail: ' + result.stderr)
                    self.assertIn('no tests', result.stderr.lower())

    def test_fake_docker_rejects_unmodeled_invocations(self):
        with tempfile.TemporaryDirectory() as directory:
            for arguments in (('unexpected-command', 'private-sentinel'), ('compose', 'frobnicate', 'private-sentinel')):
                with self.subTest(arguments=arguments):
                    result = subprocess.run([str(FIXTURES / 'docker.py'), *arguments],
                        env=os.environ | {'PYTHONPATH': str(ROOT),
                            'PATH': str(Path(sys.executable).parent) + ':' + os.defpath,
                            'NMRNET_FAKE_ENGINE_LOG': str(Path(directory) / 'calls')},
                        text=True, capture_output=True, timeout=10, check=False)
                    self.assertEqual(97, result.returncode)
                    self.assertIn('unexpected fake docker invocation:', result.stderr)
                    self.assertIn('private-sentinel', result.stderr)


if __name__ == '__main__':
    unittest.main()
