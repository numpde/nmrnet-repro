"""No fresh science dispatch may omit or replace its retained candidate identity."""

import json
import tempfile
import unittest
from dataclasses import replace

from provider_core.attempt_record import (
    LocalExecutionPhase,
    ProtectedEvidence,
    acknowledge_start,
    decode_record,
    encode_record,
    mark_execution_entered,
    require_transition,
)
from provider_core.attempt_response import ATTEMPT_RESPONSE_BYTES
from provider_core.execution import AttemptExecutor, ExecutionFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import observation, progress_response
from tests.fakes.execution import Session, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.publication import api


class PreparedIdentityRefusalTests(unittest.TestCase):
    def test_entry_requires_explicit_protected_identity(self):
        acknowledged = acknowledge_start(record(), admission())
        with self.assertRaises(TypeError):
            mark_execution_entered(acknowledged)
        for value in (None, b"unbound identity", object()):
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                mark_execution_entered(acknowledged, value)

    def test_identity_cannot_be_attached_before_execution_entry(self):
        acknowledged = acknowledge_start(record(), admission())
        with self.assertRaises(ValueError):
            replace(acknowledged, prepared_identity=ProtectedEvidence("fixture.input.v1", b"identity"))

    def test_fresh_store_transition_cannot_use_a_legacy_entry_without_identity(self):
        acknowledged = acknowledge_start(record(), admission())
        legacy = replace(acknowledged, local_phase=LocalExecutionPhase.EXECUTION_ENTERED)
        with self.assertRaises(ValueError):
            require_transition(acknowledged, legacy)

    def test_legacy_acknowledgement_keeps_its_original_byte_limit(self):
        raw = encode_record(acknowledge_start(record(), admission()))
        legacy_limit = 65536 + ATTEMPT_RESPONSE_BYTES + 256
        with self.assertRaises(ValueError):
            decode_record(raw + b" " * (legacy_limit + 1 - len(raw)), scope=scope())

    def test_v2_identity_is_required_and_checked_even_when_state_is_forged(self):
        entered = mark_execution_entered(
            acknowledge_start(record(), admission()), ProtectedEvidence("fixture.input.v1", b"identity")
        )
        document = json.loads(encode_record(entered))
        for change in (
            {"prepared_identity": None},
            {"prepared_identity": {"schema_id": "fixture.input.v1", "content": "!!!"}},
            {"prepared_identity": {"schema_id": "fixture.input.v1", "content": "eA==", "extra": True}},
            {"prepared_identity": {"schema_id": "fixture.input.v1", "content": "eB=="}},
            {"local_phase": LocalExecutionPhase.NOT_ENTERED.value},
        ):
            with self.subTest(changed_fields=tuple(change)), self.assertRaises(ValueError):
                decode_record(json.dumps(document | change).encode(), scope=scope())
        del document["prepared_identity"]
        with self.assertRaises(ValueError):
            decode_record(json.dumps(document).encode(), scope=scope())


class UnidentifiedPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unidentified_handle_retires_before_running_progress_or_execution(self):
        transport = Transport(
            reply(observation()),
            reply(response()),
            reply(progress_response()),
            reply(observation()),
        )
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            session.preparation = object()
            try:
                executor = AttemptExecutor(
                    api(transport, store.scope.api_origin),
                    store,
                    claim,
                    session,
                    allowed_job_states=frozenset({"open"}),
                    poll_seconds=1,
                    task_settle_seconds=0.1,
                )
                with self.assertRaises(ExecutionFailed) as failed:
                    await executor.run(acknowledged)
                self.assertIsInstance(failed.exception.cause, TypeError)
                self.assertEqual(["prepare", "retire"], [event[0] for event in session.events])
                self.assertEqual((acknowledged,), await store.records())
                self.assertEqual(4, len(transport.calls))
                claim.release()
            finally:
                await store.close()
