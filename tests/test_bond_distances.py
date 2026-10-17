"""Compare two released geometries with NIST experimental bond lengths."""

from tools.container_guard import require_container

require_container()

import json
import math
import unittest
from pathlib import Path


class SmallMoleculeBondDistanceTests(unittest.TestCase):
    def test_chloromethane_distances_are_consistent_with_angstrom_scale(self):
        """Check every proposed bond against experiment with geometry tolerances."""
        # Scientific sources, structure conventions, and tolerance rationale:
        # fixtures/small_chloromethanes.txt. These are experimental reference
        # lengths, not replacements for the released coordinates.
        experimental_angstrom = {
            "CH3Cl": {"H": 1.090, "Cl": 1.785},
            "CH2Cl2": {"H": 1.085, "Cl": 1.767},
        }
        tolerance_angstrom = {"H": 0.01, "Cl": 0.04}
        fixture = Path(__file__).parent / "fixtures" / "small_chloromethanes.json"
        molecules = json.loads(fixture.read_text(encoding="utf-8"))["molecules"]
        # The records have no bond graph. Supply chloromethane connectivity:
        # carbon (index 0) is singly bonded to each of the other four atoms.
        bonds = ((0, 1), (0, 2), (0, 3), (0, 4))
        for molecule in molecules:
            atoms = molecule["atoms"]
            coordinates = molecule["coordinates"]
            for left, right in bonds:
                with self.subTest(molecule=molecule["name"], bond=(left, right)):
                    distance = math.dist(coordinates[left], coordinates[right])
                    expected = experimental_angstrom[molecule["name"]][atoms[right]]
                    tolerance = tolerance_angstrom[atoms[right]]
                    print(
                        f"{molecule['name']} {left}:{atoms[left]}–{right}:{atoms[right]} "
                        f"{distance:.6f}; experimental {expected:.3f} ± {tolerance:.2f} Å",
                        flush=True,
                    )
                    self.assertAlmostEqual(
                        distance, expected, delta=tolerance,
                        msg="Released bond distance disagrees with the angstrom-scale reference",
                    )


if __name__ == "__main__":
    unittest.main()
