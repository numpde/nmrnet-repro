"""Server admission and local execution entry remain independent durable facts."""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from provider_core.attempt_record import (
    MAX_PENDING_RECORD_BYTES,
    MAX_RECORD_BYTES,
    LocalExecutionPhase,
    acknowledge_start,
    decode_record,
    encode_record,
    mark_execution_entered,
)
from provider_core.attempt_store import AttemptStore, StoreLimits
from tests.fakes.attempt_store import PREPARED_IDENTITY, admission, record, scope


class AttemptTransitionTests(unittest.IsolatedAsyncioTestCase):
    async def test_maximum_pending_record_remains_encodable_after_acknowledgement_and_entry(self):
        pending = record()
        growth = MAX_PENDING_RECORD_BYTES - len(encode_record(pending))
        large_scope = replace(scope(), api_origin=scope().api_origin + "a" * growth)
        pending = replace(pending, scope=large_scope)
        self.assertEqual(len(encode_record(pending)), MAX_PENDING_RECORD_BYTES)
        entered = mark_execution_entered(acknowledge_start(pending, admission()), PREPARED_IDENTITY)
        raw = encode_record(entered)
        self.assertLessEqual(len(raw), MAX_RECORD_BYTES)
        self.assertEqual(decode_record(raw, scope=large_scope), entered)

    async def test_acknowledgement_and_execution_entry_survive_reopen(self):
        pending = record()
        acknowledged = acknowledge_start(pending, admission())
        entered = mark_execution_entered(acknowledged, PREPARED_IDENTITY)
        self.assertEqual(acknowledged.local_phase, LocalExecutionPhase.NOT_ENTERED)
        self.assertEqual(entered.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
        self.assertEqual(entered.start.body, pending.body)
        for retained in (acknowledged, entered):
            self.assertEqual(decode_record(encode_record(retained), scope=scope()), retained)
        with tempfile.TemporaryDirectory() as directory:
            for expected, replacement in ((pending, acknowledged), (acknowledged, entered)):
                store = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(1, 4194304, 0, confirmed_history_records=64), operation_seconds=1)
                await store.open()
                try:
                    if expected == pending:
                        await store.admit(pending)
                    self.assertEqual(await store.replace(expected, replacement), replacement)
                    self.assertEqual(await store.records(), (replacement,))
                finally:
                    await store.close()
            reopened = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(1, 4194304, 0, confirmed_history_records=64), operation_seconds=1)
            await reopened.open()
            try:
                self.assertEqual(await reopened.records(), (entered,))
            finally:
                await reopened.close()

    async def test_terminal_start_replay_is_retained_without_local_execution(self):
        for state in ("succeeded", "failed", "expired"):
            acknowledged = acknowledge_start(record(), admission(state=state, replayed=True))
            restored = decode_record(encode_record(acknowledged), scope=scope())
            self.assertEqual(restored.admission.state, state)
            self.assertEqual(restored.local_phase, LocalExecutionPhase.NOT_ENTERED)
            with self.assertRaises(ValueError):
                mark_execution_entered(restored, PREPARED_IDENTITY)
