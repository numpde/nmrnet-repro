"""A changed export cannot silently become the next qualification's expectation."""

import shutil
import tempfile
import unittest
from pathlib import Path

from tests.integration.nmrnet.scientific_inputs import load_scientific_inputs


class ScientificInputRefusalTests(unittest.TestCase):
    def test_changed_source_or_historical_producer_metadata_is_refused(self):
        for suffix in (".xyz", ".manifest.json"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "scientific_inputs"
                shutil.copytree("/fixture/scientific_inputs", root)
                path = root / ("references" + suffix)
                path.write_bytes(path.read_bytes() + b" ")
                with self.assertRaisesRegex(AssertionError, "retained original export|pinned packet"):
                    load_scientific_inputs(root, "references")
