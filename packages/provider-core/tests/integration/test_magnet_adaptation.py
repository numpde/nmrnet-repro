"""Exercise a proposed Magnet adaptation through installed core runtime owners.

The local fixture models a required future preparation/prediction split. It does
not import, replace or qualify the current monolithic G01 processor or its models.
Pinned source paths and synthetic-result rationale belong to fakes/magnet.py.
"""

import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from provider_core.attempt_record import MAX_RECORD_BYTES, LocalExecutionPhase, StartPending, TerminalConfirmed
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.execution import AttemptExecutor, ExecutionFailed, ExecutionRefused
from provider_core.ownership import AttemptClaims
from provider_core.publication import TerminalPublisher
from provider_core.recovery import AttemptRecovery, ObservedAttempt
from provider_core.starting import AttemptStarter
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import scope
from tests.fakes.attempts import complete_response, observation, progress_response, retained_start, start_response
from tests.fakes.job_input import facts, response
from tests.fakes.magnet import (
    ANALYSIS_KIND,
    RESULT_SCHEMA_ID,
    SOURCE,
    IndeterminatePrediction,
    MagnetSession,
    RetrySafeUnavailable,
    prediction,
)
from tests.fakes.publication import api


class MagnetAdaptationTests(unittest.IsolatedAsyncioTestCase):
    async def test_preparation_preserves_source_and_model_fields_and_publishes_opaque_product_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            pending, store, claim = await self._open(directory)
            interpreter = AsyncMock(return_value={"smiles": "  CCO  ", "solvent": "CDCl3"})
            worker = AsyncMock(return_value=prediction())
            session = MagnetSession(store, interpreter, worker)
            expected = {"schema_id": RESULT_SCHEMA_ID, "provider_attempt_key": "retained-start:01",
                        "model_input": {"smiles": "  CCO  ", "h_solvent": "CDCl3", "c_solvent": "CDCl3"},
                        "prediction": prediction()}
            expected_bytes = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
            transport = Transport(reply(start_response(analysis_kind_ref=ANALYSIS_KIND)), *self._run_replies(), reply(complete_response(
                result_schema_id=RESULT_SCHEMA_ID, result_fingerprint="sha256:" + hashlib.sha256(expected_bytes).hexdigest(),
                result_byte_length=len(expected_bytes))))
            client = api(transport, store.scope.api_origin)
            try:
                acknowledged = await AttemptStarter(client, store, claim).start(pending)
                terminal = await self._executor(client, store, claim, session).run(acknowledged)
                confirmed = await TerminalPublisher(client, store, claim).publish(terminal)
                self.assertIsInstance(confirmed, TerminalConfirmed)
                interpreter.assert_awaited_once_with(SOURCE)
                worker.assert_awaited_once_with({"smiles": "  CCO  ", "h_solvent": "CDCl3", "c_solvent": "CDCl3"})
                sent = json.loads(transport.calls[-1][0].content)
                content = base64.b64decode(sent["canonical_result_base64"])
                self.assertEqual(content, expected_bytes)
                self.assertEqual(json.loads(content), expected)
                self.assertEqual(json.loads(content)["prediction"]["future_binding_field"], {"retain": "exact product-owned content"})
                self.assertEqual(await store.records(), (confirmed,))
                self.assertEqual(session.phases, [LocalExecutionPhase.NOT_ENTERED, LocalExecutionPhase.EXECUTION_ENTERED])
                self.assertTrue(session.retired)
                claim.release()
            finally:
                await store.close()

    async def test_typed_preentry_unavailability_remains_retry_safe_without_a_new_core_outcome_type(self):
        with tempfile.TemporaryDirectory() as directory:
            pending, store, claim = await self._open(directory)
            unavailable = RetrySafeUnavailable("scripted interpreter unavailable before prediction")
            interpreter = AsyncMock(side_effect=unavailable)
            worker = AsyncMock()
            session = MagnetSession(store, interpreter, worker)
            transport = Transport(reply(start_response(analysis_kind_ref=ANALYSIS_KIND)), *self._run_replies(preparation_only=True))
            client = api(transport, store.scope.api_origin)
            try:
                acknowledged = await AttemptStarter(client, store, claim).start(pending)
                executor = self._executor(client, store, claim, session)
                with self.assertRaises(ExecutionFailed) as failed:
                    await executor.run(acknowledged)
                self.assertIs(failed.exception.cause, unavailable)
                self.assertEqual(await store.records(), (acknowledged,))
                self.assertIs(acknowledged.local_phase, LocalExecutionPhase.NOT_ENTERED)
                worker.assert_not_awaited()
                self.assertTrue(session.retired)
                self.assertEqual(len(transport.calls), 4)
                # Retry is the composition's explicit choice of a new owned session
                # for the identical immutable request, never an automatic core loop.
                retry_worker = AsyncMock(return_value=prediction())
                retry_session = MagnetSession(store, AsyncMock(return_value={"smiles": "  CCO  ", "solvent": "CDCl3"}), retry_worker)
                retry_client = api(Transport(*self._run_replies()), store.scope.api_origin)
                terminal = await self._executor(retry_client, store, claim, retry_session).run(acknowledged)
                self.assertIs(terminal.attempt.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
                retry_worker.assert_awaited_once()
                claim.release()
            finally:
                await store.close()

    async def test_indeterminate_prediction_retains_entered_phase_and_recovery_never_reexecutes(self):
        with tempfile.TemporaryDirectory() as directory:
            pending, store, claim = await self._open(directory)
            indeterminate = IndeterminatePrediction("scripted prediction reply lost")
            worker = AsyncMock(side_effect=indeterminate)
            session = MagnetSession(store, AsyncMock(return_value={"smiles": "  CCO  ", "solvent": "CDCl3"}), worker)
            client = api(Transport(reply(start_response(analysis_kind_ref=ANALYSIS_KIND)), *self._run_replies()), store.scope.api_origin)
            try:
                acknowledged = await AttemptStarter(client, store, claim).start(pending)
                with self.assertRaises(ExecutionFailed) as failed:
                    await self._executor(client, store, claim, session).run(acknowledged)
                self.assertIs(failed.exception.cause, indeterminate)
                (entered,) = await store.records()
                self.assertIs(entered.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
                self.assertTrue(session.retired)
                claim.release()
            finally:
                await store.close()
            reopened = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1)
            await reopened.open()
            try:
                (retained,) = await reopened.records()
                recovered_claim, = AttemptClaims().claim_records((retained,))
                observed_client = api(Transport(reply(observation())), reopened.scope.api_origin)
                result = await AttemptRecovery(observed_client, reopened, recovered_claim).recover(retained)
                self.assertIsInstance(result, ObservedAttempt)
                with self.assertRaises(ExecutionRefused):
                    await self._executor(observed_client, reopened, recovered_claim, session).run(retained)
                worker.assert_awaited_once()
                recovered_claim.release()
            finally:
                await reopened.close()

    async def _open(self, directory):
        pending = StartPending(scope(), ANALYSIS_KIND, facts(SOURCE), retained_start())
        store = AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1)
        await store.open()
        return pending, store, AttemptClaims().claim_start(pending)

    def _executor(self, client, store, claim, session):
        return AttemptExecutor(client, store, claim, session, poll_seconds=1, task_settle_seconds=0.1)

    def _run_replies(self, *, preparation_only=False):
        beginning = (reply(observation()), reply(response(SOURCE)), reply(progress_response()))
        if preparation_only:
            return beginning
        return (*beginning, reply(observation()), reply(progress_response(phase="running")),
                reply(observation()), reply(observation()))
