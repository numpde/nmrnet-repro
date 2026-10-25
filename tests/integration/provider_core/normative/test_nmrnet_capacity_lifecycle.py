"""The fixed capacity-input recipe preserves its authored H128/C128 population."""

import unittest

from tests.integration.nmrnet.reference_fixtures import require_capacity_inputs


class CapacityInputTests(unittest.TestCase):
    def test_fixed_128_atom_input_recipe_is_capacity_only(self):
        manifest = capacity_manifest()
        require_capacity_inputs(manifest)
        self.assertEqual([len(item["original_request"]["atoms"]) for item in manifest["records"]], [128, 128])


def capacity_manifest():
    return {
        "records": [
            {
                "source_element": element,
                "source_key_hex": "313238",
                "original_request": {
                    "coordinate_unit": "angstrom",
                    "atoms": [element] * 128,
                    "coordinates": [[index * 0.1, 0.0, 0.0] for index in range(128)],
                },
            }
            for element in ("H", "C")
        ]
    }
