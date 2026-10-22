"""Archival frees active slots while preserving original confirmed evidence bytes."""

import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from provider_core.attempt_files import history_directory_name, record_name
from provider_core.attempt_record import MAX_EVIDENCE_BYTES, MAX_RECORD_BYTES, decode_record, encode_record
from provider_core.attempt_store import AttemptStore, StoreLimits, StoreRefused
from tests.fakes.attempt_history import retain_confirmed
from tests.fakes.attempt_store import record, scope


class ConfirmedHistoryTests(unittest.IsolatedAsyncioTestCase):
    def store(self, root):
        return AttemptStore(root, scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1)

    async def test_maximum_evidence_keeps_original_noncanonical_bytes_and_releases_only_its_active_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root)
            await store.open()
            confirmed = await retain_confirmed(store, record(), evidence=b"e" * MAX_EVIDENCE_BYTES)
            raw = json.dumps(json.loads(encode_record(confirmed)), indent=1).encode() + b"\n "
            active = root / record_name(confirmed)
            active.write_bytes(raw)
            history = root / history_directory_name(scope()) / active.name
            try:
                self.assertEqual(await store.archive_confirmed(confirmed), confirmed)
                self.assertEqual(await store.records(), ())
                self.assertEqual(await store.admission_inventory(), ())
                self.assertFalse(active.exists())
                self.assertEqual(history.read_bytes(), raw)
                self.assertEqual(decode_record(history.read_bytes(), scope=scope()), confirmed)
                self.assertEqual(stat.S_IMODE(history.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(history.stat().st_mode), 0o600)
                self.assertEqual(history.stat().st_nlink, 1)
                with self.assertRaisesRegex(StoreRefused, "archived"):
                    await store.admit(record())
                await store.admit(record("fresh:start"))
                self.assertEqual(await store.records(), (record("fresh:start"),))
                self.assertEqual(history.read_bytes(), raw)
            finally:
                await store.close()

    async def test_multiple_archives_survive_reopen_with_one_active_slot_and_old_keys_never_become_fresh(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            confirmed_records = []
            for index in range(3):
                store = self.store(root)
                await store.open()
                try:
                    self.assertEqual(await store.records(), ())
                    confirmed = await retain_confirmed(store, record(f"history:{index}"), evidence=b"private evidence")
                    confirmed_records.append(confirmed)
                    await store.archive_confirmed(confirmed)
                    # Explicit repeat observes the same retained archive, not another copy or lifecycle transition.
                    self.assertEqual(await store.archive_confirmed(confirmed), confirmed)
                    with self.assertRaisesRegex(StoreRefused, "archived"):
                        await store.admit(record(f"history:{index}"))
                finally:
                    await store.close()
            history = root / history_directory_name(scope())
            self.assertEqual(len(list(history.iterdir())), 3)
            for confirmed in confirmed_records:
                self.assertEqual(decode_record((history / record_name(confirmed)).read_bytes(), scope=scope()), confirmed)

    async def test_complete_identical_copy_can_finish_explicit_archival_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root)
            await store.open()
            confirmed = await retain_confirmed(store, record(), evidence=b"original evidence")
            active = root / record_name(confirmed)
            history = root / history_directory_name(scope())
            history.mkdir(mode=0o700)
            (history / active.name).write_bytes(active.read_bytes())
            (history / active.name).chmod(0o600)
            await store.close()
            reopened = self.store(root)
            await reopened.open()
            try:
                self.assertEqual(await reopened.records(), (confirmed,))
                # The already-complete copy needs no new allocation. Finishing
                # removal must work even when the copy consumed all headroom.
                with patch("provider_core.attempt_files.os.fstatvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)):
                    await reopened.archive_confirmed(confirmed)
                self.assertEqual(await reopened.records(), ())
                self.assertFalse(active.exists())
                self.assertEqual((history / active.name).stat().st_nlink, 1)
            finally:
                await reopened.close()
