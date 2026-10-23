"""Stale, foreign or backward transitions cannot replace retained obligations."""

import asyncio
import fcntl
import json
import os
import stat
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from provider_core.attempt_record import acknowledge_start, decode_record, encode_record, mark_execution_entered
from provider_core.attempt_store import AttemptStore, StoreFailed, StoreLimits, StoreRefused
from tests.fakes.attempt_store import PREPARED_IDENTITY, admission, record, scope


class AttemptTransitionFailureTests(unittest.IsolatedAsyncioTestCase):
    def store(self, root):
        return AttemptStore(root, scope=scope(), limits=StoreLimits(1, 4194304, 0, confirmed_history_records=64), operation_seconds=1)

    async def test_foreign_acknowledgement_and_repeated_entry_are_rejected(self):
        for changes in (
            {"job_ref": "job:foreign"},
            {"provider_ref": "provider:foreign"},
            {"analysis_kind_ref": "different_kind"},
            {"execution_attempt_ref": "private-invalid-reference"},
        ):
            with self.assertRaises(ValueError):
                acknowledge_start(record(), replace(admission(), **changes))
        with self.assertRaises(ValueError):
            mark_execution_entered(
                mark_execution_entered(acknowledge_start(record(), admission()), PREPARED_IDENTITY), PREPARED_IDENTITY
            )

    async def test_stale_absent_foreign_and_backward_replacements_preserve_current_record(self):
        pending = record()
        acknowledged = acknowledge_start(pending, admission())
        entered = mark_execution_entered(acknowledged, PREPARED_IDENTITY)
        with tempfile.TemporaryDirectory() as directory:
            store = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(1, 4194304, 0, confirmed_history_records=64), operation_seconds=1)
            await store.open()
            try:
                with self.assertRaises(StoreRefused):
                    await store.replace(pending, acknowledged)
                await store.admit(pending)
                for expected, replacement in (
                    (pending, entered),
                    (pending, acknowledge_start(record("another:key"), admission())),
                    (acknowledged, entered),
                ):
                    with self.assertRaises((ValueError, StoreRefused)):
                        await store.replace(expected, replacement)
                self.assertEqual(await store.records(), (pending,))
                await store.replace(pending, acknowledged)
                with self.assertRaises(StoreRefused):
                    await store.replace(pending, acknowledged)
                await store.replace(acknowledged, entered)
                with self.assertRaises((ValueError, StoreRefused)):
                    await store.replace(entered, acknowledged)
                self.assertEqual(await store.records(), (entered,))
            finally:
                await store.close()

    async def test_corrupt_acknowledged_records_cannot_be_restored_as_executable(self):
        good = json.loads(encode_record(acknowledge_start(record(), admission())))
        for changes in (
            {"local_phase": "private-canary"},
            {"admission": {}},
            {"start": {}},
            {"extra": "private-canary"},
            {"admission": good["admission"] | {"state": "succeeded"}, "local_phase": "execution_entered"},
        ):
            with self.assertRaises(ValueError) as caught:
                decode_record(json.dumps(good | changes).encode(), scope=scope())
            self.assertNotIn("private-canary", str(caught.exception))

    async def test_replacement_failure_preserves_original_or_visible_entered_record(self):
        original_sync = os.fsync
        failure = OSError("private-replacement-canary")

        def fail_staging_sync(fd):
            if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY:
                raise failure
            return original_sync(fd)

        def fail_directory_sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise failure
            return original_sync(fd)

        for operation, effect, visible in (
            ("write", failure, False),
            ("fsync", fail_staging_sync, False),
            ("replace", failure, False),
            ("fsync", fail_directory_sync, True),
        ):
            with self.subTest(operation=operation, visible=visible), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pending = record()
                acknowledged = acknowledge_start(pending, admission())
                entered = mark_execution_entered(acknowledged, PREPARED_IDENTITY)
                store = self.store(root)
                await store.open()
                try:
                    await store.admit(pending)
                    await store.replace(pending, acknowledged)
                    with (
                        patch("provider_core.attempt_files.os." + operation, side_effect=effect),
                        self.assertRaises(StoreFailed) as caught,
                    ):
                        await store.replace(acknowledged, entered)
                    self.assertEqual(caught.exception.record_visible, visible)
                    self.assertIs(caught.exception.cause, failure)
                    self.assertNotIn("private-replacement-canary", str(caught.exception))
                    target = next(root.glob("*.json"))
                    self.assertEqual(decode_record(target.read_bytes(), scope=scope()), entered if visible else acknowledged)
                    with self.assertRaises(StoreRefused):
                        await store.replace(acknowledged, entered)
                finally:
                    await store.close()
                reopened = self.store(root)
                try:
                    if visible:
                        await reopened.open()
                        (restored,) = await reopened.records()
                        self.assertEqual(restored, entered)
                        with self.assertRaises(ValueError):
                            mark_execution_entered(restored, PREPARED_IDENTITY)
                    else:
                        self.assertTrue(list(root.glob(".pending-*")))
                        with self.assertRaises(StoreFailed):
                            await reopened.open()
                finally:
                    await reopened.close()

    async def test_cancelled_replacement_settles_once_before_storage_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(Path(directory))
            pending = record()
            acknowledged = acknowledge_start(pending, admission())
            await store.open()
            await store.admit(pending)
            reached, release = threading.Event(), threading.Event()
            original_replace = os.replace

            def delayed_replace(*args, **kwargs):
                reached.set()
                if not release.wait(2):
                    raise TimeoutError("test replacement was not released")
                return original_replace(*args, **kwargs)

            try:
                with patch("provider_core.attempt_files.os.replace", side_effect=delayed_replace) as replacements:
                    task = asyncio.create_task(store.replace(pending, acknowledged))
                    self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    with self.assertRaises(StoreRefused):
                        await store.replace(pending, acknowledged)
                    with self.assertRaises(StoreRefused):
                        await store.close()
                    release.set()
                    self.assertEqual((await store.settle()).value, acknowledged)
                    self.assertEqual(replacements.call_count, 1)
                self.assertEqual(await store.records(), (acknowledged,))
            finally:
                release.set()
                await store.close()

    async def test_replacement_headroom_covers_growth_and_staging_without_new_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(Path(directory))
            pending = record()
            acknowledged = acknowledge_start(pending, admission())
            await store.open()
            try:
                await store.admit(pending)
                required = 2 * 4194304 - len(encode_record(pending))
                with (
                    patch(
                        "provider_core.attempt_files.os.fstatvfs", return_value=SimpleNamespace(f_bavail=required - 1, f_frsize=1)
                    ),
                    self.assertRaises(StoreRefused),
                ):
                    await store.replace(pending, acknowledged)
                self.assertEqual(await store.records(), (pending,))
                with patch(
                    "provider_core.attempt_files.os.fstatvfs", return_value=SimpleNamespace(f_bavail=required, f_frsize=1)
                ):
                    await store.replace(pending, acknowledged)
                self.assertEqual(await store.records(), (acknowledged,))
            finally:
                await store.close()
