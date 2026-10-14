"""Observe the maintained repository lane's effective kernel grants.

Port of all eight RuntimePostureTests at
https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_container_posture.py#L18
Identity and numeric resources follow tools/container.sh: caller ownership,
512 MiB, two CPUs, 64 PIDs and zero swap. The launcher injects expected identity;
reading the process's own UID as the expected value would erase this proof.
Magnet's fixture-install assertion observes temporary build trees in this test
image. Inference-image contents still require that image's separate import lane;
the raw Python test image cannot certify its contents.
"""

from tools.container_guard import require_container

require_container()

import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def mount_record(destination):
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        separator = fields.index('-')
        if fields[4] == destination:
            return fields[separator + 1], set(fields[5].split(',')) | set(fields[separator + 3].split(','))
    raise AssertionError(f'Cannot verify runtime mount posture: {destination} has no mountinfo record.')


def process_status():
    return dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())


class RuntimePostureTests(unittest.TestCase):
    def test_runs_as_explicit_non_root_caller_identity(self):
        for variable, actual in (('NMRNET_EXPECTED_UID', os.getuid()), ('NMRNET_EXPECTED_GID', os.getgid())):
            expected = os.environ.get(variable)
            self.assertIsNotNone(expected, f'Cannot verify caller identity: the maintained launcher must inject {variable}.')
            self.assertTrue(expected.isdecimal(), f'Cannot verify caller identity: {variable} must contain a numeric identity.')
            self.assertNotEqual(int(expected), 0)
            self.assertEqual(actual, int(expected))

    def test_root_is_read_only_and_tmp_is_writable(self):
        self.assertIn('ro', mount_record('/')[1])
        with tempfile.NamedTemporaryFile(dir='/tmp') as probe:
            probe.write(b'expected')
            probe.flush()
            self.assertEqual(Path(probe.name).read_bytes(), b'expected')

    def test_has_no_effective_linux_capabilities(self):
        self.assertEqual(int(process_status()['CapEff'].strip(), 16), 0)

    def test_kernel_enforces_no_new_privileges(self):
        self.assertEqual(process_status()['NoNewPrivs'].strip(), '1')

    def test_has_only_loopback_network_interface(self):
        self.assertEqual({path.name for path in Path('/sys/class/net').iterdir()}, {'lo'})

    def test_tmp_is_a_hardened_writable_tmpfs(self):
        filesystem, options = mount_record('/tmp')
        self.assertEqual(filesystem, 'tmpfs')
        self.assertTrue({'rw', 'noexec', 'nosuid', 'nodev'} <= options)

    def test_cgroup_v2_enforces_resource_bounds(self):
        cgroup = Path('/sys/fs/cgroup')
        for name, expected in (('pids.max', '64'), ('memory.max', '536870912'), ('memory.swap.max', '0')):
            self.assertEqual((cgroup / name).read_text().strip(), expected, name)
        quota, period = map(int, (cgroup / 'cpu.max').read_text().split())
        self.assertEqual(quota, 2 * period)

    def test_fixture_install_retains_no_build_source(self):
        for name in ('/build/models/embedded_fixture', '/build/test-products', '/fixture-stage', '/opt/build'):
            self.assertFalse(Path(name).exists(), name)
        directory = Path('/build')
        if directory.exists():
            self.assertFalse(any(path.is_file() for path in directory.rglob('*')))


if __name__ == '__main__':
    unittest.main()
