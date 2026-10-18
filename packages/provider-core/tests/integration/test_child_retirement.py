"""Distinguish cancelled tasks/closed IPC from an exact scripted child stopping.

This qualifies the core session seam, not a sibling's scientific worker/protocol,
process tree, source selection, native parser, warm reuse or production isolation.
"""

import asyncio
import base64
import itertools
import json
import tempfile
import unittest
from contextlib import AsyncExitStack

from provider_core.attempt_record import LocalExecutionPhase, TerminalOperation, TerminalPending
from provider_core.execution import AttemptExecutor, ExecutionFailed
from provider_core.ownership import OwnershipRefused
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.child_session import ChildChannelClosed, ChildSession, ChildStopUnconfirmed
from tests.fakes.execution import acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.publication import api


class ChildRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordered_success_retains_result_and_reaps_child_before_returning_publication(self):
        async with AsyncExitStack() as cleanup:
            store, acknowledged, claim, session = await self._resources(cleanup, "success")
            executor = self._executor(store, claim, session)
            terminal = await self._run(cleanup, executor, acknowledged)
            self.assertIsInstance(terminal, TerminalPending)
            self.assertIs(terminal.operation, TerminalOperation.COMPLETE)
            self.assertEqual(session.phases, [LocalExecutionPhase.NOT_ENTERED, LocalExecutionPhase.EXECUTION_ENTERED])
            self.assertEqual(session.generations, 1)
            self.assertTrue(session.child.pipes_closed)
            self.assertIsNotNone(session.child.process.returncode)
            body = json.loads(terminal.body)
            result = json.loads(base64.b64decode(body["canonical_result_base64"], validate=True))
            self.assertEqual(result, {"state": "result", "source_fingerprint": acknowledged.start.input.input_fingerprint,
                                      "value": "scripted-only"})
            self.assertEqual(await store.records(), (terminal,))
            claim.release()

    async def test_scripted_preparation_rejection_never_records_entry_or_generates(self):
        async with AsyncExitStack() as cleanup:
            store, acknowledged, claim, session = await self._resources(cleanup, "reject")
            executor = self._executor(store, claim, session, preparation_only=True)
            terminal = await self._run(cleanup, executor, acknowledged)
            self.assertIs(terminal.operation, TerminalOperation.FAIL)
            self.assertIs(terminal.attempt.local_phase, LocalExecutionPhase.NOT_ENTERED)
            self.assertEqual(session.generations, 0)
            self.assertIsNotNone(session.child.process.returncode)
            self.assertTrue(session.child.pipes_closed)
            claim.release()

    async def test_cancelled_generation_stops_exact_child_and_keeps_entered_history_without_publication(self):
        async with AsyncExitStack() as cleanup:
            store, acknowledged, claim, session = await self._resources(cleanup, "block")
            executor = self._executor(store, claim, session)
            task = asyncio.create_task(executor.run(acknowledged))
            cleanup.push_async_callback(self._cancel_executor, task)
            await asyncio.wait_for(session.generation_started.wait(), 2)
            original_process = session.child.process
            self.assertIsNone(original_process.returncode)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await self._finished(task)
            self.assertIs(session.child.process, original_process)
            self.assertIsNotNone(original_process.returncode)
            self.assertTrue(session.child.pipes_closed)
            self.assertEqual(session.generations, 1)
            self.assertIsNone(executor.last_failure.outcome)
            self.assertIs((await store.records())[0].local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
            claim.release()

    async def test_closed_output_does_not_release_claim_until_that_same_child_is_stopped_and_reaped(self):
        async with AsyncExitStack() as cleanup:
            store, acknowledged, claim, session = await self._resources(cleanup, "close_output")
            executor = self._executor(store, claim, session)
            with self.assertRaises(ExecutionFailed) as failed:
                await self._run(cleanup, executor, acknowledged)
            original_process = session.child.process
            self.assertIsInstance(failed.exception.cause, ChildChannelClosed)
            self.assertIsNone(original_process.returncode)
            self.assertTrue(any(isinstance(cause, ChildStopUnconfirmed) for cause in failed.exception.cleanup_failures))
            with self.assertRaises(OwnershipRefused):
                claim.release()
            with self.assertRaises(ExecutionFailed):
                await executor.settle()
            self.assertIsNone(original_process.returncode)
            self.assertEqual(session.generations, 1)
            await session.child.stop()
            with self.assertRaises(OwnershipRefused):
                claim.release()
            self.assertIsNone(await executor.settle())
            self.assertIs(session.child.process, original_process)
            self.assertIsNotNone(original_process.returncode)
            self.assertTrue(session.child.pipes_closed)
            self.assertEqual(session.generations, 1)
            self.assertIs((await store.records())[0].local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
            claim.release()

    async def _resources(self, cleanup, mode):
        directory = cleanup.enter_context(tempfile.TemporaryDirectory())
        store, acknowledged, claim = await acknowledged_store(directory)
        cleanup.push_async_callback(store.close)
        session = ChildSession(store, mode)
        cleanup.push_async_callback(session.child.stop)
        return store, acknowledged, claim, session

    async def _cancel_executor(self, task):
        if not task.done():
            task.cancel()
            await asyncio.wait((task,), timeout=1)
        if not task.done():
            raise TimeoutError("Test executor did not settle after cancellation; still attempt exact-child and store cleanup")
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self, cleanup, executor, acknowledged):
        task = asyncio.create_task(executor.run(acknowledged))
        cleanup.push_async_callback(self._cancel_executor, task)
        return await self._finished(task)

    async def _finished(self, task):
        # wait_for would await cancellation indefinitely if retirement regressed.
        # Retain the task for bounded cleanup even when this watchdog expires.
        await asyncio.wait((task,), timeout=3)
        if not task.done():
            raise TimeoutError("Test executor did not finish; cancel it and clean up its retained child")
        return task.result()

    def _executor(self, store, claim, session, *, preparation_only=False):
        beginning = (reply(observation()), reply(response()), reply(progress_response()), reply(observation()))
        after = () if preparation_only else (reply(progress_response(phase="running")),)
        # Long poll interval isolates child/caller-cancellation correctness from
        # scheduler-dependent read counts; all waits have separate test watchdogs.
        transport = Transport(*itertools.chain(beginning, after, itertools.repeat(reply(observation()), 16)))
        return AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                               allowed_job_states=frozenset({"open"}), poll_seconds=10, task_settle_seconds=1)
