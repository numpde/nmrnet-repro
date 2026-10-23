"""Failed terminal persistence preserves the prior obligation and protected evidence."""

import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from provider_core.attempt_record import (
    MAX_RECORD_BYTES,
    ProtectedEvidence,
    TerminalHeld,
    TerminalOperation,
    TerminalPending,
    acknowledge_start,
    decode_record,
)
from provider_core.attempt_request import fail_body
from provider_core.attempt_store import AttemptStore, StoreFailed, StoreLimits, StoreRefused
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF


class TerminalStorageFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_evidence_sync_keeps_previous_record_and_staged_evidence(self):
        pending = record()
        active = acknowledge_start(pending, admission())
        terminal = TerminalPending(
            active,
            TerminalOperation.FAIL,
            fail_body(ATTEMPT_REF, "input_rejected", "Correct the input"),
            ProtectedEvidence("evidence.v1", b"private-required-evidence"),
        )
        original_sync = os.fsync

        def failed_staging_sync(fd):
            if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY:
                raise OSError("private-sync-cause")
            return original_sync(fd)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AttemptStore(root, scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=1)
            await store.open()
            try:
                await store.admit(pending)
                await store.replace(pending, active)
                with (
                    patch("provider_core.attempt_files.os.fsync", side_effect=failed_staging_sync),
                    self.assertRaises(StoreFailed),
                ):
                    await store.replace(active, terminal)
                self.assertEqual(decode_record(next(root.glob("*.json")).read_bytes(), scope=scope()), active)
                self.assertEqual(decode_record(next(root.glob(".pending-*")).read_bytes(), scope=scope()), terminal)
                with self.assertRaises(StoreRefused):
                    await store.records()
            finally:
                await store.close()

    async def test_conflict_hold_survives_reopen_and_cannot_be_changed_back_to_pending(self):
        pending = record()
        active = acknowledge_start(pending, admission())
        terminal = TerminalPending(
            active,
            TerminalOperation.FAIL,
            fail_body(ATTEMPT_REF, "input_rejected", "Correct the input"),
            ProtectedEvidence("evidence.v1", b"original required evidence"),
        )
        held = TerminalHeld(terminal, "execution_attempt_failure_after_success", "request:example")
        with tempfile.TemporaryDirectory() as directory:
            for first in (True, False):
                store = AttemptStore(
                    Path(directory), scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=1
                )
                await store.open()
                try:
                    if first:
                        await store.admit(pending)
                        await store.replace(pending, active)
                        await store.replace(active, terminal)
                        await store.replace(terminal, held)
                    self.assertEqual(await store.records(), (held,))
                    with self.assertRaises(StoreRefused):
                        await store.replace(held, terminal)
                finally:
                    await store.close()
