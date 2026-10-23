"""Capacity observation preserves records and does not reserve a start."""

import tempfile
import unittest
from pathlib import Path

from provider_core.attempt_store import AttemptStore, StoreLimits
from tests.fakes.attempt_store import record, scope


class AdmissionInventoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_returns_the_exact_inventory_without_consuming_a_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AttemptStore(root, scope=scope(), limits=StoreLimits(2, 4194304, 0, confirmed_history_records=64), operation_seconds=1)
            await store.open()
            try:
                self.assertEqual(await store.admission_inventory(owner=self), ())
                self.assertEqual(list(root.iterdir()), [])
                await store.admit(record())
                before = {path.name: path.read_bytes() for path in root.iterdir()}
                self.assertEqual(await store.admission_inventory(owner=self), (record(),))
                self.assertEqual(await store.admission_inventory(owner=self), (record(),))
                self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)
                await store.admit(record("retained:two"))
                self.assertEqual(len(await store.records()), 2)
            finally:
                await store.close()
