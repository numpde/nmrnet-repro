"""Changed populations, coordinates, producer fields or order fail capacity-input admission."""

import unittest

from tests.integration.nmrnet.reference_fixtures import require_capacity_inputs
from tests.integration.nmrnet.normative.test_nmrnet_capacity_lifecycle import capacity_manifest


class CapacityInputRefusalTests(unittest.TestCase):
    def test_smaller_changed_or_reordered_capacity_input_cannot_pass(self):
        for change in ("smaller", "coordinate", "producer", "order"):
            manifest = capacity_manifest()
            if change == "smaller":
                manifest["records"][0]["original_request"]["atoms"].pop()
                manifest["records"][0]["original_request"]["coordinates"].pop()
            elif change == "coordinate":
                manifest["records"][0]["original_request"]["coordinates"][127][0] = 12.0
            elif change == "producer":
                manifest["records"][0]["source_key_hex"] = "00"
            else:
                manifest["records"].reverse()
            with self.subTest(change=change), self.assertRaises(AssertionError):
                require_capacity_inputs(manifest)
