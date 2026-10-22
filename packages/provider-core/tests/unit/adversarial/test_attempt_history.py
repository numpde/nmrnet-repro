"""Unresolved records, foreign scopes and interrupted archival cannot become free work."""

import asyncio
import fcntl
import os
import stat
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from provider_core.attempt_files import history_directory_name, record_name
from provider_core.attempt_record import MAX_RECORD_BYTES
from provider_core.attempt_store import AttemptStore, StoreFailed, StoreLimits, StoreRefused
from tests.fakes.attempt_history import retain_confirmed
from tests.fakes.attempt_store import record, scope


class ConfirmedHistoryFailureTests(unittest.IsolatedAsyncioTestCase):
    def store(self, root, *, recovery_scope):
        return AttemptStore(root, scope=recovery_scope, limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1)

    async def test_new_archive_requires_copy_headroom_before_creating_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root, recovery_scope=scope())
            await store.open()
            confirmed = await retain_confirmed(store, record(), evidence=b"protected original")
            active = root / record_name(confirmed)
            raw = active.read_bytes()
            try:
                with (
                    patch("provider_core.attempt_files.os.fstatvfs", return_value=SimpleNamespace(f_bavail=0, f_frsize=4096)),
                    self.assertRaisesRegex(StoreRefused, "headroom"),
                ):
                    await store.archive_confirmed(confirmed)
                self.assertEqual(active.read_bytes(), raw)
                self.assertFalse((root / history_directory_name(scope())).exists())
                self.assertEqual(await store.records(), (confirmed,))
            finally:
                await store.close()

    async def test_history_directory_must_remain_private_and_cannot_be_a_symlink(self):
        for kind in ("readable", "writable", "symlink", "file"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = self.store(root, recovery_scope=scope())
                await store.open()
                confirmed = await retain_confirmed(store, record(), evidence=b"protected original")
                active = root / record_name(confirmed)
                raw = active.read_bytes()
                await store.close()
                history = root / history_directory_name(scope())
                if kind == "symlink":
                    history.symlink_to(root, target_is_directory=True)
                elif kind == "file":
                    history.write_bytes(b"not a directory")
                    history.chmod(0o600)
                else:
                    history.mkdir(mode=0o700)
                    history.chmod(0o755 if kind == "readable" else 0o777)
                reopened = self.store(root, recovery_scope=scope())
                try:
                    with self.assertRaises(StoreFailed):
                        await reopened.open()
                    self.assertEqual(active.read_bytes(), raw)
                finally:
                    await reopened.close()

    async def test_pending_terminal_command_cannot_be_archived_even_when_its_send_may_have_committed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(Path(directory), recovery_scope=scope())
            await store.open()
            confirmed = await retain_confirmed(store, record(), evidence=b"protected evidence")
            try:
                for unresolved in (record(), confirmed.pending.attempt, confirmed.pending):
                    with self.subTest(kind=type(unresolved).__name__), self.assertRaises(StoreRefused):
                        await store.archive_confirmed(unresolved)
                self.assertEqual(await store.records(), (confirmed,))
                self.assertEqual(len(list(Path(directory).iterdir())), 1)
            finally:
                await store.close()

    async def test_history_binds_empty_active_root_to_its_scope_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root, recovery_scope=scope())
            await store.open()
            confirmed = await retain_confirmed(store, record(), evidence=b"protected evidence")
            await store.archive_confirmed(confirmed)
            await store.close()
            for changed in (
                replace(scope(), generation_id="other"),
                replace(scope(), consumer_id="other"),
                replace(scope(), provider_ref="provider:other"),
                replace(scope(), api_origin="https://other.example"),
            ):
                with self.subTest(scope=changed):
                    other = self.store(root, recovery_scope=changed)
                    try:
                        with self.assertRaises(StoreFailed):
                            await other.open()
                    finally:
                        await other.close()
            reopened = self.store(root, recovery_scope=scope())
            await reopened.open()
            try:
                self.assertEqual(await reopened.records(), ())
                with self.assertRaisesRegex(StoreRefused, "archived"):
                    await reopened.admit(record())
            finally:
                await reopened.close()

    async def test_partial_conflicting_or_aliased_archive_cannot_hide_the_active_original(self):
        for kind in ("partial", "conflicting", "hardlink", "symlink"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                store = self.store(root, recovery_scope=scope())
                await store.open()
                confirmed = await retain_confirmed(store, record(), evidence=b"protected original")
                active = root / record_name(confirmed)
                raw = active.read_bytes()
                history = root / history_directory_name(scope())
                history.mkdir(mode=0o700)
                target = history / active.name
                if kind == "hardlink":
                    os.link(active, target)
                elif kind == "symlink":
                    target.symlink_to(active)
                else:
                    target.write_bytes(raw[:31] if kind == "partial" else raw + b" ")
                    target.chmod(0o600)
                await store.close()
                reopened = self.store(root, recovery_scope=scope())
                try:
                    with self.assertRaises(StoreFailed):
                        await reopened.open()
                    self.assertEqual(active.read_bytes(), raw)
                    self.assertTrue(target.exists() or target.is_symlink())
                finally:
                    await reopened.close()

    async def test_cancelled_archive_wait_retains_original_future_and_only_its_owner_can_settle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root, recovery_scope=scope())
            await store.open()
            confirmed = await retain_confirmed(store, record(), evidence=b"protected original")
            original_raw = (root / record_name(confirmed)).read_bytes()
            entered, release = threading.Event(), threading.Event()
            original = os.write

            def held_write(fd, raw):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("Test archive write was not released")
                return original(fd, raw)

            owner = object()
            task = None
            try:
                with patch("provider_core.attempt_files.os.write", side_effect=held_write):
                    task = asyncio.create_task(store.archive_confirmed(confirmed, owner=owner))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    with self.assertRaises(StoreRefused):
                        await store.records()
                    with self.assertRaises(StoreRefused):
                        await store.close()
                    with self.assertRaises(StoreRefused):
                        await store.settle()
                    self.assertIs(store.pending_owner, owner)
                    release.set()
                    settled = await store.settle(owner=owner)
                self.assertEqual(settled.value, confirmed)
                self.assertEqual(await store.records(), ())
                self.assertEqual((root / history_directory_name(scope()) / record_name(confirmed)).read_bytes(), original_raw)
            finally:
                release.set()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
                if store.pending_operation is not None:
                    await store.settle(owner=owner)
                await store.close()

    async def test_failed_copy_sync_and_active_removal_preserve_truth_and_original_evidence(self):
        for boundary in ("write", "file_sync", "history_sync", "active_unlink", "active_sync"):
            with self.subTest(boundary=boundary):
                await self.check_failed_archive_boundary(boundary)

    async def check_failed_archive_boundary(self, boundary):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.store(root, recovery_scope=scope())
            await store.open()
            confirmed = await retain_confirmed(store, record(), evidence=b"protected original")
            active = root / record_name(confirmed)
            original_raw = active.read_bytes()
            history = root / history_directory_name(scope()) / active.name
            failure = OSError("PRIVATE_ARCHIVE_FAILURE")
            original_sync, original_write, original_unlink = os.fsync, os.write, os.unlink
            removed = False

            def sync(fd):
                is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
                if (
                    (
                        boundary == "file_sync"
                        and not is_directory
                        and fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY
                    )
                    or (boundary == "history_sync" and is_directory and fd != store._files.fd)
                    or (boundary == "active_sync" and is_directory and fd == store._files.fd and removed)
                ):
                    raise failure
                return original_sync(fd)

            def write(fd, raw):
                if boundary == "write":
                    raise failure
                return original_write(fd, raw)

            def unlink(name, *, dir_fd):
                nonlocal removed
                if boundary == "active_unlink":
                    raise failure
                original_unlink(name, dir_fd=dir_fd)
                removed = True

            try:
                with (
                    patch("provider_core.attempt_files.os.fsync", side_effect=sync),
                    patch("provider_core.attempt_files.os.write", side_effect=write),
                    patch("provider_core.attempt_files.os.unlink", side_effect=unlink),
                    self.assertRaises(StoreFailed) as caught,
                ):
                    await store.archive_confirmed(confirmed)
                self.assertIs(caught.exception.cause, failure)
                self.assertTrue(caught.exception.record_visible)
                self.assertNotIn("PRIVATE", str(caught.exception))
                if boundary == "active_sync":
                    self.assertFalse(active.exists())
                    self.assertIn("removal was observed", str(caught.exception))
                else:
                    self.assertEqual(active.read_bytes(), original_raw)
                if boundary != "write":
                    self.assertEqual(history.read_bytes(), original_raw)
                with self.assertRaises(StoreRefused):
                    await store.records()
            finally:
                await store.close()
            reopened = self.store(root, recovery_scope=scope())
            try:
                if boundary == "write":
                    with self.assertRaises(StoreFailed):
                        await reopened.open()
                    self.assertEqual(active.read_bytes(), original_raw)
                else:
                    await reopened.open()
                    await reopened.archive_confirmed(confirmed)
                    self.assertEqual(await reopened.records(), ())
                    self.assertEqual(history.read_bytes(), original_raw)
            finally:
                await reopened.close()

    async def test_both_directory_closes_are_attempted_once_and_independent_failures_remain_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self.store(Path(directory), recovery_scope=scope())
            await store.open()
            confirmed = await retain_confirmed(store, record(), evidence=b"protected original")
            await store.archive_confirmed(confirmed)
            descriptors = (store._files.history_fd, store._files.fd)
            failures = {descriptor: OSError(f"PRIVATE_CLOSE_{index}") for index, descriptor in enumerate(descriptors)}
            attempted = []
            original_close = os.close

            def uncertain_close(fd):
                attempted.append(fd)
                original_close(fd)
                raise failures[fd]

            with (
                patch("provider_core.attempt_files.os.close", side_effect=uncertain_close),
                self.assertRaises(StoreFailed) as caught,
            ):
                await store.close()
            self.assertEqual(attempted, list(descriptors))
            self.assertIs(caught.exception.cause, failures[descriptors[0]])
            self.assertEqual(caught.exception.cleanup_failures, (failures[descriptors[1]],))
            self.assertFalse(store.retirement_confirmed)
            with self.assertRaises(StoreRefused):
                await store.close()
