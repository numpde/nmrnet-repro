"""Full or uncertain storage cannot authorize fresh allocation."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from provider_core.attempt_files import AttemptFiles
from provider_core.attempt_record import TerminalConfirmed
from provider_core.attempt_response import read_fail
from provider_core.attempt_store import AttemptStore, StoreFailed, StoreLimits, StoreRefused
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF, fail_response
from tests.fakes.publication import retained_failure


class AdmissionInventoryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_confirmed_records_still_fill_durable_slots_and_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            store, terminal = await retained_failure(directory, scope())
            try:
                receipt = read_fail(
                    fail_response(),
                    execution_attempt_ref=ATTEMPT_REF,
                    failure_code="input_rejected",
                    failure_message=" Explain\n雪\x01 ",
                )
                confirmed = TerminalConfirmed(terminal, receipt)
                await store.replace(terminal, confirmed)
                before = {path.name: path.read_bytes() for path in Path(directory).iterdir()}
                with self.assertRaisesRegex(StoreRefused, "slots"):
                    await store.admission_inventory(owner=self)
                self.assertEqual(await store.records(), (confirmed,))
                self.assertEqual({path.name: path.read_bytes() for path in Path(directory).iterdir()}, before)
            finally:
                await store.close()

    async def test_free_space_is_rechecked_on_real_admission_after_successful_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(2, 4194304, 0), operation_seconds=1)
            await store.open()
            try:
                self.assertEqual(await store.admission_inventory(owner=self), ())
                empty_disk = SimpleNamespace(f_bavail=0, f_frsize=4096)
                with patch("provider_core.attempt_files.os.fstatvfs", return_value=empty_disk):
                    with self.assertRaisesRegex(StoreRefused, "headroom"):
                        await store.admission_inventory(owner=self)
                    with self.assertRaisesRegex(StoreRefused, "headroom"):
                        await store.admit(record())
                self.assertEqual(await store.records(), ())
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                await store.close()

    async def test_preflight_covers_existing_growth_new_obligation_and_staging(self):
        record_budget = 4194304
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AttemptStore(root, scope=scope(), limits=StoreLimits(2, record_budget, 0), operation_seconds=1)
            await store.open()
            try:
                await store.admit(record())
                before = {path.name: path.read_bytes() for path in root.iterdir()}
                # This tiny pending start must eventually fit a full outcome.
                # Two budgets cannot also cover a new full record and staging.
                self.assertLess(sum(map(len, before.values())), record_budget)
                with (
                    patch(
                        "provider_core.attempt_files.os.fstatvfs",
                        return_value=SimpleNamespace(f_bavail=2 * record_budget, f_frsize=1),
                    ),
                    self.assertRaisesRegex(StoreRefused, "headroom"),
                ):
                    await store.admission_inventory(owner=self)
                with patch(
                    "provider_core.attempt_files.os.fstatvfs",
                    return_value=SimpleNamespace(f_bavail=3 * record_budget, f_frsize=1),
                ):
                    self.assertEqual(await store.admission_inventory(owner=self), (record(),))
                self.assertEqual({path.name: path.read_bytes() for path in root.iterdir()}, before)
            finally:
                await store.close()

    async def test_failed_capacity_observation_preserves_private_cause_and_holds_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(2, 4194304, 0), operation_seconds=1)
            await store.open()
            try:
                cause = OSError("PRIVATE disk observation")
                with (
                    patch("provider_core.attempt_files.os.fstatvfs", side_effect=cause),
                    self.assertRaises(StoreFailed) as caught,
                ):
                    await store.admission_inventory(owner=self)
                self.assertIs(caught.exception.cause, cause)
                self.assertFalse(caught.exception.record_visible)
                self.assertNotIn("PRIVATE", str(caught.exception))
                with self.assertRaises(StoreRefused):
                    await store.admit(record())
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                await store.close()

    async def test_cancelled_preflight_retains_original_io_until_exact_owner_settles(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(2, 4194304, 0), operation_seconds=1)
            await store.open()
            entered, release = threading.Event(), threading.Event()
            original = AttemptFiles.admission_inventory

            def held(files):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("Fixture preflight was not released")
                return original(files)

            try:
                with patch.object(AttemptFiles, "admission_inventory", held):
                    task = asyncio.create_task(store.admission_inventory(owner=self))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIs(store.pending_owner, self)
                    with self.assertRaises(StoreRefused):
                        await store.admit(record())
                    with self.assertRaises(StoreRefused):
                        await store.close()
                    with self.assertRaises(StoreRefused):
                        await store.settle(owner=object())
                    release.set()
                    settled = await store.settle(owner=self)
                self.assertEqual((settled.operation, settled.value), ("check fresh Attempt storage capacity", ()))
                self.assertEqual(await store.records(), ())
                self.assertEqual(list(Path(directory).iterdir()), [])
            finally:
                release.set()
                if store.pending_operation is not None:
                    await store.settle(owner=self)
                await store.close()
