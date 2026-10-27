"""One operator selection drives preparation and reuses existing model trees."""

from tools.container_guard import require_container

require_container()

import unittest

from tests.unit.provider_prepare_fakes import PreparationFixture


class ProviderPreparationTests(unittest.TestCase):
    def test_literal_choices_reach_all_owners_and_existing_extractions_are_kept(self):
        fixture = PreparationFixture()
        self.addCleanup(fixture.close)
        sentinel = fixture.root / "executed"
        selection = f"../shared wheels $(touch {sentinel})"
        expected = str((fixture.root / selection).resolve())
        first = fixture.run(selection)
        self.assertEqual(0, first.returncode, first.stderr)
        self.assertFalse(sentinel.exists())
        self.assertNotIn("NEXT_FROM_CHILD", first.stdout + first.stderr)
        calls = fixture.calls()
        self.assertEqual(9, len(calls))
        owners = [call[0] for call in calls]
        self.assertLess(owners.index("wheelhouse.sh:download"), owners.index("inference_image.sh:build"))
        self.assertLess(owners.index("wheelhouse.sh:download"), owners.index("container.sh:geometry/image/build"))
        self.assertNotIn("wheelhouse.sh:download provider-core", owners)
        self.assertNotIn("wheelhouse.sh:download preparation", owners)
        self.assertNotIn("container.sh:delivery/image/build", owners)
        self.assertTrue(all(root == expected and interface == "lo" for _, root, interface, _ in calls))
        self.assertEqual(["weight.zip", "weight.zip", "checkpoints.zip", "checkpoints.zip"], [call[3] for call in calls[-4:]])
        receipts = [fixture.root / ".work/prepared" / name / "provenance.json" for name in ("weight", "checkpoints")]
        original = [(path.read_bytes(), path.stat().st_ino) for path in receipts]
        fixture.journal.unlink()
        second = fixture.run(selection)
        self.assertEqual(0, second.returncode, second.stderr)
        self.assertEqual(5, len(fixture.calls()))
        self.assertEqual(original, [(path.read_bytes(), path.stat().st_ino) for path in receipts])
