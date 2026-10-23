"""Unsafe storage cannot silently discard obligations or release pending I/O."""

import asyncio
import os
import stat
import tempfile
import threading
import unittest
from dataclasses import replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from provider_core.attempt_store import AttemptStore, StoreFailed, StoreLimits, StoreRefused
from tests.fakes.attempt_store import record, scope


class AttemptStoreFailureTests(unittest.IsolatedAsyncioTestCase):
    def store(self, root, *, seconds=1, slots=2):
        return AttemptStore(root, scope=scope(), limits=StoreLimits(slots, 4194304, 0, confirmed_history_records=64), operation_seconds=seconds)

    async def test_oversized_record_is_refused_before_storage_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root)
            await store.open()
            oversized = replace(record(), scope=replace(scope(), api_origin="https://" + "a" * 70000))
            try:
                with self.assertRaises(StoreRefused):
                    await store.admit(oversized)
                self.assertEqual(list(root.iterdir()), [])
                await store.admit(record())
                self.assertEqual(await store.records(), (record(),))
            finally:
                await store.close()

    async def test_competing_owner_and_capacity_cannot_replace_retained_start(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = self.store(root, slots=1), self.store(root)
            await first.open()
            try:
                with self.assertRaises(StoreRefused):
                    await second.open()
                await first.admit(record())
                with self.assertRaises(StoreRefused):
                    await first.admit(record("retained:two"))
                self.assertEqual(await first.records(), (record(),))
            finally:
                await first.close()
                await second.close()

    async def test_corrupt_unknown_symlink_and_nonregular_entries_are_not_empty_inventory(self):
        for kind in ("corrupt", "unknown", "symlink", "fifo"):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / ("a" * 64 + ".json")
                if kind == "unknown":
                    (root / "unexpected").write_bytes(b"private-canary")
                elif kind == "symlink":
                    path.symlink_to("missing")
                elif kind == "fifo":
                    os.mkfifo(path, 0o600)
                else:
                    path.write_bytes(b"private-canary")
                    path.chmod(0o600)
                store = self.store(root)
                try:
                    with self.assertRaises(StoreFailed) as caught:
                        await store.open()
                    self.assertNotIn("private-canary", str(caught.exception))
                finally:
                    await store.close()

    async def test_cancelled_wait_retains_worker_and_lock_until_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root)
            await store.open()
            entered, release = threading.Event(), threading.Event()
            original = os.write

            def delayed_write(fd, data):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test write was not released")
                return original(fd, data)

            try:
                with patch("provider_core.attempt_files.os.write", side_effect=delayed_write):
                    task = asyncio.create_task(store.admit(record()))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    with self.assertRaisesRegex(StoreRefused, "unobserved I/O result; settle that operation"):
                        await store.records()
                    with self.assertRaises(StoreRefused):
                        await store.close()
                    self.assertFalse(store.retirement_confirmed)
                    release.set()
                    settled = await store.settle()
                    self.assertEqual((settled.operation, settled.value), ("retain pending start", record()))
                self.assertEqual(await store.records(), (record(),))
            finally:
                release.set()
                await store.close()
            with self.assertRaisesRegex(StoreRefused, "closed Attempt storage; this store instance cannot be reopened"):
                await store.records()

    async def test_file_close_failure_cannot_be_hidden_by_successful_directory_close(self):
        original = os.close

        def uncertain_file_close(fd):
            is_file = stat.S_ISREG(os.fstat(fd).st_mode)
            original(fd)
            if is_file:
                raise OSError("private-close-canary")

        for reading in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                store = self.store(Path(directory))
                await store.open()
                if reading:
                    await store.admit(record())
                with (
                    patch("provider_core.attempt_files.os.close", side_effect=uncertain_file_close),
                    self.assertRaises(StoreFailed),
                ):
                    if reading:
                        await store.records()
                    else:
                        await store.admit(record())
                with self.assertRaises(StoreFailed):
                    await store.close()
                self.assertFalse(store.retirement_confirmed)

    async def test_publication_failures_preserve_evidence_and_block_further_work(self):
        original_sync = os.fsync
        failure = OSError("private-publication-canary")

        def failed_sync(fd, *, directory):
            directory_fd = stat.S_ISDIR(os.fstat(fd).st_mode)
            if directory_fd == directory:
                raise failure
            return original_sync(fd)

        for boundary in ("write", "file_sync", "link", "unlink", "directory_sync"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = self.store(root)
                await store.open()
                operation = "fsync" if boundary.endswith("sync") else boundary
                replacement = partial(failed_sync, directory=boundary == "directory_sync") if operation == "fsync" else failure
                try:
                    with (
                        patch("provider_core.attempt_files.os." + operation, side_effect=replacement),
                        self.assertRaises(StoreFailed) as caught,
                    ):
                        await store.admit(record())
                    self.assertIs(caught.exception.cause, failure)
                    self.assertNotIn("private-publication-canary", str(caught.exception))
                    visible = boundary in ("unlink", "directory_sync")
                    self.assertEqual(caught.exception.record_visible, visible)
                    self.assertEqual(len(list(root.glob("*.json"))), int(visible))
                    self.assertTrue(list(root.iterdir()))
                    with self.assertRaises(StoreRefused):
                        await store.admit(record())
                finally:
                    await store.close()
                reopened = self.store(root)
                try:
                    if boundary == "directory_sync":
                        await reopened.open()
                        self.assertEqual(await reopened.records(), (record(),))
                    else:
                        with self.assertRaises(StoreFailed):
                            await reopened.open()
                finally:
                    await reopened.close()

    async def test_write_failure_keeps_primary_and_uncertain_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(Path(directory))
            await store.open()
            primary, cleanup = OSError("private-write"), OSError("private-close")
            original_close = os.close

            def failed_close(fd):
                original_close(fd)
                raise cleanup

            with (
                patch("provider_core.attempt_files.os.write", side_effect=primary),
                patch("provider_core.attempt_files.os.close", side_effect=failed_close),
                self.assertRaises(StoreFailed) as caught,
            ):
                await store.admit(record())
            self.assertIs(caught.exception.cause, primary)
            self.assertEqual(caught.exception.cleanup_failures, (cleanup,))
            with self.assertRaises(StoreFailed):
                await store.close()
            self.assertFalse(store.retirement_confirmed)

    async def test_headroom_and_conflicting_key_refusals_preserve_existing_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(Path(directory))
            await store.open()
            try:
                await store.admit(record())
                with self.assertRaises(StoreRefused):
                    await store.admit(replace(record(), analysis_kind_ref="different_kind"))
                with patch("provider_core.attempt_files.os.fstatvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)):
                    with self.assertRaises(StoreRefused):
                        await store.admit(record("retained:two"))
                    self.assertEqual(await store.admit(record()), record())
                self.assertEqual(await store.records(), (record(),))
            finally:
                await store.close()

    async def test_expired_wait_settles_original_write_without_resubmitting(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(Path(directory), seconds=0.05)
            await store.open()
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            original = os.write

            def delayed_write(fd, data):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test write was not released")
                try:
                    return original(fd, data)
                finally:
                    finished.set()

            try:
                with patch("provider_core.attempt_files.os.write", side_effect=delayed_write) as writes:
                    with self.assertRaises(StoreFailed):
                        await store.admit(record())
                    self.assertTrue(entered.is_set())
                    self.assertEqual(store.pending_operation, "retain pending start")
                    with self.assertRaises(StoreRefused):
                        await store.admit(record())
                    release.set()
                    self.assertTrue(await asyncio.to_thread(finished.wait, 1))
                    self.assertEqual((await store.settle()).value, record())
                    self.assertEqual(writes.call_count, 1)
            finally:
                release.set()
                await store.close()
