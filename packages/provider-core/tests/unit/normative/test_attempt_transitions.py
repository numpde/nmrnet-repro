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
from provider_core.checkpoint_store import CheckpointLimits, CheckpointStore
from provider_core.producer_checkpoint import EvidenceDigest
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
        live = acknowledge_start(pending, admission())
        self.assertIs(live.local_phase, LocalExecutionPhase.NOT_ENTERED)
        self.assertIs(mark_execution_entered(live, PREPARED_IDENTITY).local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
        digest = EvidenceDigest.from_evidence(PREPARED_IDENTITY)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = CheckpointStore(root, scope=scope(), limits=CheckpointLimits(1, 300000), operation_seconds=1)
            await store.open()
            try:
                await store.retain_start(pending)
                acknowledged = await store.acknowledge_start(pending, admission())
            finally:
                await store.close()
            store = CheckpointStore(root, scope=scope(), limits=CheckpointLimits(1, 300000), operation_seconds=1)
            await store.open()
            try:
                self.assertEqual(await store.inventory(), (acknowledged,))
                entered = await store.mark_entered(acknowledged, digest)
            finally:
                await store.close()
            store = CheckpointStore(root, scope=scope(), limits=CheckpointLimits(1, 300000), operation_seconds=1)
            await store.open()
            try:
                self.assertEqual(await store.inventory(), (entered,))
                self.assertEqual(entered.prepared, digest)
                self.assertEqual(entered.acknowledgement.start.body, pending.body)
                self.assertNotIn(PREPARED_IDENTITY.content, (root / "checkpoints.sqlite3").read_bytes())
            finally:
                await store.close()

    async def test_terminal_start_replay_is_retained_without_local_execution(self):
        for state in ("succeeded", "failed", "expired"):
            acknowledged = acknowledge_start(record(), admission(state=state, replayed=True))
            restored = decode_record(encode_record(acknowledged), scope=scope())
            self.assertEqual(restored.admission.state, state)
            self.assertEqual(restored.local_phase, LocalExecutionPhase.NOT_ENTERED)
            with self.assertRaises(ValueError):
                mark_execution_entered(restored, PREPARED_IDENTITY)
