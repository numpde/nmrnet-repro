"""The final admitted candidate is retained before science may consume its handle."""

import json
import tempfile
import unittest
from dataclasses import replace

from provider_core.analysis import PreparedAnalysis
from provider_core.attempt_record import (
    MAX_ACKNOWLEDGED_BYTES,
    MAX_EVIDENCE_BYTES,
    LocalExecutionPhase,
    ProtectedEvidence,
    acknowledge_start,
    decode_record,
    encode_record,
    mark_execution_entered,
)
from provider_core.execution import AttemptExecutor
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import observation, progress_response
from tests.fakes.execution import Session, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.publication import api

IDENTITY = ProtectedEvidence("fixture.prepared_input.v1", b"private candidate identity")


class PreparedIdentityTests(unittest.IsolatedAsyncioTestCase):
    def test_new_entry_round_trips_identity_while_legacy_entry_stays_legacy(self):
        acknowledged = acknowledge_start(record(), admission())
        legacy = replace(acknowledged, local_phase=LocalExecutionPhase.EXECUTION_ENTERED)
        entered = mark_execution_entered(acknowledged, IDENTITY)
        for value, schema in ((legacy, "provider_core.acknowledged.v1"), (entered, "provider_core.acknowledged.v2")):
            with self.subTest(schema=schema):
                raw = encode_record(value)
                self.assertEqual(schema, json.loads(raw)["schema_id"])
                fields = {"schema_id", "start", "admission", "local_phase"}
                if value is entered:
                    fields.add("prepared_identity")
                self.assertEqual(fields, set(json.loads(raw)))
                restored = decode_record(raw, scope=scope())
                self.assertEqual(value, restored)
                self.assertEqual(raw, encode_record(restored))
                self.assertNotIn("private candidate", repr(restored))
        self.assertIsNone(legacy.prepared_identity)
        self.assertEqual(IDENTITY, entered.prepared_identity)

    def test_largest_identity_and_escaped_schema_fit_acknowledgement_bound(self):
        identity = ProtectedEvidence("\U0001f600" * 1024, b"x" * MAX_EVIDENCE_BYTES)
        entered = mark_execution_entered(acknowledge_start(record(), admission()), identity)
        raw = encode_record(entered)
        self.assertLessEqual(len(raw), MAX_ACKNOWLEDGED_BYTES)
        self.assertEqual(entered, decode_record(raw, scope=scope()))

    async def test_execute_observes_matching_identity_in_durable_entry_and_terminal(self):
        transport = Transport(
            reply(observation()),
            reply(response()),
            reply(progress_response()),
            reply(observation()),
            reply(progress_response(phase="running")),
            reply(observation()),
            reply(observation()),
        )
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            session.handle = session.preparation = PreparedAnalysis(IDENTITY)
            try:
                terminal = await AttemptExecutor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                ).run(acknowledged)
                self.assertEqual(IDENTITY, session.events[1][1].prepared_identity)
                self.assertEqual(IDENTITY, terminal.attempt.prepared_identity)
                self.assertEqual(IDENTITY, decode_record(encode_record(terminal), scope=scope()).attempt.prepared_identity)
                claim.release()
            finally:
                await store.close()
