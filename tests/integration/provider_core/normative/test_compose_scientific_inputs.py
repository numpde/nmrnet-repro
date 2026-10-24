"""Historical producer identity remains separate from current scientific execution."""

import unittest
from pathlib import Path

from tests.integration.nmrnet.scientific_inputs import load_scientific_inputs


class ScientificInputTests(unittest.TestCase):
    def test_retained_inputs_match_original_reference_population_and_capacity_recipe(self):
        for case, counts in (("references", [42, 38, 40, 27]), ("capacity", [128, 128])):
            with self.subTest(case=case):
                source, manifest = load_scientific_inputs(Path("/fixture/scientific_inputs"), case)
                self.assertEqual([len(item["original_request"]["atoms"]) for item in manifest["records"]], counts)
                self.assertEqual(manifest["source"]["size"], len(source))
                self.assertEqual(manifest["producer"]["execution"]["image_id"],
                                 "sha256:cabb4fe3e36541b0fefe54ca7c16fb21f9a60112a1e3d47992d4aa18343ecd08")
