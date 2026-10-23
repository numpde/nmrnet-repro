"""Private storage retains exact pending starts across explicit close/reopen."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_core.attempt_store import AttemptStore, StoreLimits
from tests.fakes.attempt_store import record, scope


class AttemptStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_duplicate_and_reopen_retain_exact_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for run in range(2):
                store = AttemptStore(root, scope=scope(), limits=StoreLimits(2, 4194304, 0, confirmed_history_records=64), operation_seconds=1)
                await store.open()
                if run == 0:
                    self.assertEqual(await store.records(), ())
                self.assertEqual(await store.admit(record()), record())
                self.assertEqual(await store.admit(record()), record())
                self.assertEqual(await store.records(), (record(),))
                self.assertEqual(len(list(root.iterdir())), 1)
                await store.close()
                self.assertTrue(store.retirement_confirmed)

    async def test_short_writes_retain_the_complete_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(2, 4194304, 0, confirmed_history_records=64), operation_seconds=1)
            await store.open()
            original = os.write

            def short_write(fd, data):
                return original(fd, data[:17])

            try:
                with patch("provider_core.attempt_files.os.write", side_effect=short_write) as writes:
                    await store.admit(record())
                self.assertGreater(writes.call_count, 1)
                self.assertEqual(await store.records(), (record(),))
            finally:
                await store.close()
