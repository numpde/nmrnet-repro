"""Retaining interrupted outcomes never repeats science or releases uncertain work."""

import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from provider_core.analysis import AnalysisFailure
from provider_core.attempt_record import LocalExecutionPhase, ProtectedEvidence, SuppressedOutcome, SuppressionReason
from provider_core.execution import AttemptExecutor, ExecutionFailed, ExecutionStopped
from provider_core.ownership import OwnershipRefused
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.execution import Session, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.publication import api


async def stopped_executor(directory, retirement_failure):
    store, acknowledged, claim = await acknowledged_store(directory)
    session = Session(store)
    session.retirement_failure = retirement_failure
    transport = Transport(reply(observation()), reply(response()), reply(progress_response()), reply(observation()),
                          reply(progress_response(phase="running")), reply(observation()), reply(observation(job_state="closed")))
    executor = AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                               poll_seconds=1, task_settle_seconds=0.1)
    try:
        await executor.run(acknowledged)
    except ExecutionFailed as failure:
        if not isinstance(failure.cause, ExecutionStopped):
            raise
    else:
        raise AssertionError("Fixture did not stop the completed result's acceptance")
    return store, executor, session, claim, transport


class SuppressionPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_caller_cancellation_preserves_returned_result_after_original_api_settles(self):
        reading = asyncio.Event()

        class PendingTransport(Transport):
            async def exchange(self, request, *, maximum_bytes, owner=None):
                if len(self.calls) == 6:
                    self.calls.append((request, maximum_bytes))
                    self.pending_owner = owner
                    self.retirement_confirmed = False
                    reading.set()
                    await asyncio.Future()
                return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            transport = PendingTransport(reply(observation()), reply(response()), reply(progress_response()), reply(observation()),
                                         reply(progress_response(phase="running")), reply(observation()))
            executor = AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                                       poll_seconds=1, task_settle_seconds=0.1)
            try:
                task = asyncio.create_task(executor.run(acknowledged))
                await asyncio.wait_for(reading.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                with self.assertRaises(ExecutionFailed) as unretained:
                    await executor.settle()
                self.assertIn("retain_suppressed_outcome()", str(unretained.exception.cleanup_failures[-1]))
                retained = await executor.retain_suppressed_outcome()
                self.assertIs(retained.reason, SuppressionReason.CALLER_CANCELLED)
                self.assertIsInstance(executor.last_failure.cause, asyncio.CancelledError)
                self.assertEqual(retained.candidate.evidence, session.outcome.evidence)
                self.assertEqual(len(transport.calls), 7)
                claim.release()
            finally:
                await store.close()

    async def test_classified_preparation_failure_can_be_retained_after_stop_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            session.preparation = AnalysisFailure("invalid_input", "Supply coordinates.", ProtectedEvidence("evidence.v1", b"reason"))
            transport = Transport(reply(observation()), reply(response()), reply(progress_response()),
                                  reply(observation(job_state="closed")))
            executor = AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                                       poll_seconds=1, task_settle_seconds=0.1)
            try:
                with self.assertRaises(ExecutionFailed):
                    await executor.run(acknowledged)
                retained = await executor.retain_suppressed_outcome()
                self.assertIs(retained.candidate.attempt.local_phase, LocalExecutionPhase.NOT_ENTERED)
                self.assertIs(retained.reason, SuppressionReason.SERVER_STOP)
                self.assertEqual(retained.candidate.evidence, session.preparation.evidence)
                self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
                self.assertEqual(len(transport.calls), 4)
                claim.release()
            finally:
                await store.close()

    async def test_cancelled_suppression_write_settles_original_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store, executor, session, claim, transport = await stopped_executor(directory, None)
            primary = executor.last_failure.cause
            reached, release = threading.Event(), threading.Event()
            original = os.replace
            writes = 0

            def delayed(*args, **kwargs):
                nonlocal writes
                writes += 1
                reached.set()
                if not release.wait(2):
                    raise TimeoutError("Fixture did not release original suppression write")
                return original(*args, **kwargs)

            try:
                with patch("provider_core.attempt_files.os.replace", side_effect=delayed):
                    task = asyncio.create_task(executor.retain_suppressed_outcome())
                    self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIs(executor.last_failure.cause, primary)
                    self.assertIsInstance(executor.last_failure.unconfirmed_record, SuppressedOutcome)
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    release.set()
                    self.assertIsNone(await executor.settle())
                retained, = await store.records()
                self.assertIsInstance(retained, SuppressedOutcome)
                self.assertEqual(retained.candidate.evidence, session.outcome.evidence)
                self.assertEqual(writes, 1)
                self.assertEqual(len(transport.calls), 7)
                self.assertEqual([event[0] for event in session.events].count("execute"), 1)
                claim.release()
            finally:
                release.set()
                await store.close()

    async def test_failed_suppression_write_keeps_original_cause_and_outcome_owned(self):
        with tempfile.TemporaryDirectory() as directory:
            store, executor, session, claim, transport = await stopped_executor(directory, None)
            primary = executor.last_failure.cause
            private = OSError("private suppression disk failure")
            try:
                with patch("provider_core.attempt_files.os.replace", side_effect=private), self.assertRaises(ExecutionFailed) as failed:
                    await executor.retain_suppressed_outcome()
                self.assertIs(failed.exception.cause, primary)
                self.assertIs(failed.exception.outcome, session.outcome)
                self.assertIs(failed.exception.cleanup_failures[-1].cause, private)
                self.assertNotIn(str(private), str(failed.exception))
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                with self.assertRaises(ExecutionFailed):
                    await executor.settle()
                self.assertEqual(len(transport.calls), 7)
                self.assertEqual([event[0] for event in session.events].count("execute"), 1)
            finally:
                await store.close()

    async def test_durable_suppression_cannot_release_unconfirmed_worker_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            store, executor, session, claim, transport = await stopped_executor(directory, RuntimeError("private cleanup failure"))
            try:
                retained = await executor.retain_suppressed_outcome()
                self.assertEqual(await store.records(), (retained,))
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                session.retirement_failure = None
                self.assertIsNone(await executor.settle())
                self.assertEqual(len(transport.calls), 7)
                self.assertEqual([event[0] for event in session.events].count("execute"), 1)
                claim.release()
            finally:
                await store.close()
