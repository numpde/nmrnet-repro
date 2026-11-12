"""Checked-in acquisition source follows its own pin, independent of SDK wheels."""

from pathlib import Path
import runpy
import unittest

ROOT = Path.cwd()


class AcquisitionSourceTests(unittest.TestCase):
    def test_checked_in_source_matches_its_reviewed_pin(self):
        bundle = ROOT / 'host/bootstrap/acquisition'
        updater = runpy.run_path(str(bundle / 'acquisition_tool/source_update.py'))
        manifest = updater['verify'](bundle)
        pin = updater['read_pin'](ROOT / 'requirements/provider-tools-source.json')
        self.assertEqual(manifest['release_version'], pin['version'])
