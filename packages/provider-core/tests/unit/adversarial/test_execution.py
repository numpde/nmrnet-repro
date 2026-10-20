"""Interrupted entry never invokes science; outcomes and cleanup remain owned."""

import asyncio
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from provider_core.attempt_record import LocalExecutionPhase, TerminalPending
from provider_core.attempt_store import StoreRefused
from provider_core.controller import AttemptController
from provider_core.execution import AttemptExecutor, ExecutionFailed, ExecutionRefused, ExecutionStopped
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.controller import empty_store
from tests.fakes.execution import Session, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.publication import api


def normal_transport(*, last_observation=None):
    return Transport(
        reply(observation()),
        reply(response()),
        reply(progress_response()),
        reply(observation()),
        reply(progress_response(phase="running")),
        reply(last_observation or observation()),
        reply(observation()),
    )


class ExecutionFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_settlement_preserves_the_same_cleanup_interruption_context(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = Session(store)
            previous = ExchangeFailed("Authored owned cleanup evidence", primary=OSError("PRIVATE cause"))
            interruption = asyncio.CancelledError()
            interruption.__cause__ = previous
            session.retirement_failure = interruption
            transport = Transport(reply(start_response(state="failed", replayed=True)))
            controller = AttemptController(
                api(transport, store.scope.api_origin),
                store,
                AttemptClaims(),
                session,
                allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
            )
            try:
                with self.assertRaises(asyncio.CancelledError) as first:
                    await controller.run(record())
                self.assertIs(first.exception, interruption)
                with self.assertRaises(asyncio.CancelledError) as second:
                    await controller.settle()
                self.assertIs(second.exception, interruption)
                self.assertIs(interruption.__cause__, previous)
                self.assertEqual(len(transport.calls), 1)
                session.retirement_failure = None
                await controller.settle()
            finally:
                await store.close()

    async def test_late_cancelled_child_updates_the_same_escaped_failure_after_timed_out_join(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            entered, cancelled, release = (asyncio.Event() for _ in range(3))
            previous = OSError("PRIVATE prior model cause")
            originals = []

            async def prepare(_acknowledged, _instructions):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as interruption:
                    originals.append(interruption)
                    cancelled.set()
                    await release.wait()
                    raise interruption from previous

            executor = AttemptExecutor(
                api(normal_transport(), store.scope.api_origin),
                store,
                claim,
                session,
                allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.01,
            )
            try:
                with patch.object(session, "prepare", prepare):
                    task = asyncio.create_task(executor.run(acknowledged))
                    await asyncio.wait_for(entered.wait(), 1)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await task
                    await asyncio.wait_for(cancelled.wait(), 1)
                    retained = executor.last_failure
                    self.assertIs(caught.exception.__cause__, retained)
                    self.assertIs(retained.cause, caught.exception)
                    self.assertIsNone(retained.operation_interruption)
                    self.assertIsInstance(retained.cleanup_failures[0], TimeoutError)
                    release.set()
                    await executor.settle()
                    self.assertIs(executor.last_failure, retained)
                    self.assertIs(retained.operation_interruption, originals[0])
                    self.assertIs(retained.operation_interruption.__cause__, previous)
                    self.assertIs(caught.exception.__cause__, retained)
                    self.assertEqual([event[0] for event in session.events], ["retire"])
                    claim.release()
            finally:
                release.set()
                await store.close()

    async def test_api_primary_and_cleanup_cancellation_keep_both_exact_causes(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            previous = OSError("PRIVATE prior cleanup cause")
            cleanup = asyncio.CancelledError()
            cleanup.__cause__ = previous
            session.retirement_failure = cleanup
            transport = Transport(reply(observation()), ExchangeFailed("Authored input-response failure"))
            client = api(transport, store.scope.api_origin)
            executor = AttemptExecutor(
                client, store, claim, session, allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1
            )
            try:
                with self.assertRaises(asyncio.CancelledError) as caught:
                    await executor.run(acknowledged)
                self.assertIs(caught.exception, cleanup)
                retained = caught.exception.__cause__
                self.assertIs(retained, executor.last_failure)
                self.assertIs(retained.cause, client.last_failure)
                self.assertIs(retained.api_failure, client.last_failure)
                self.assertIs(retained.interruption_cause, previous)
                self.assertEqual(retained.cleanup_failures, (cleanup,))
                self.assertEqual(len(transport.calls), 2)
                session.retirement_failure = None
                await executor.settle()
                self.assertIs(executor.last_failure, retained)
                self.assertEqual([event[0] for event in session.events], ["retire", "retire"])
                claim.release()
            finally:
                await store.close()

    async def test_unconfirmed_running_progress_prevents_entry_and_execution(self):
        transport = Transport(
            reply(observation()),
            reply(response()),
            reply(progress_response()),
            reply(observation()),
            ExchangeFailed("running reply lost"),
        )
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin), store, claim, session,
                allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1
            )
            try:
                with self.assertRaises(ExecutionFailed):
                    await executor.run(acknowledged)
                self.assertEqual(await store.records(), (acknowledged,))
                self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
                self.assertTrue(executor.last_failure.api_failure.attempt_mutation_may_have_committed)
                claim.release()
            finally:
                await store.close()

    async def test_unencodable_result_is_preserved_and_retired_without_reexecution(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            session.outcome = replace(session.outcome, content=b"x" * 786433)
            executor = AttemptExecutor(
                api(normal_transport(), store.scope.api_origin), store, claim, session,
                allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1
            )
            try:
                with self.assertRaises(ExecutionFailed) as failed:
                    await executor.run(acknowledged)
                self.assertIs(failed.exception.outcome, session.outcome)
                self.assertIsInstance(failed.exception.cause, ValueError)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                with self.assertRaises(ExecutionFailed):
                    await executor.settle()
                (retained,) = await store.records()
                self.assertIs(retained.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
            finally:
                await store.close()

    async def test_definite_entry_refusal_retires_without_retaining_a_nonexistent_write(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            executor = AttemptExecutor(
                api(normal_transport(), store.scope.api_origin), store, claim, session,
                allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1
            )
            try:
                with (
                    patch.object(store, "replace", side_effect=StoreRefused("storage admission refused")),
                    self.assertRaises(ExecutionFailed) as failed,
                ):
                    await executor.run(acknowledged)
                self.assertIsInstance(failed.exception.cause, StoreRefused)
                self.assertIsNone(failed.exception.unconfirmed_record)
                self.assertEqual(await store.records(), (acknowledged,))
                self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
                claim.release()
            finally:
                await store.close()

    async def test_cancelled_entry_write_is_observed_but_never_resumed_into_science(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            session.retirement_failure = RuntimeError("retirement confirmation unavailable")
            transport = normal_transport()
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin), store, claim, session,
                allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1
            )
            reached, release = threading.Event(), threading.Event()
            original = os.replace

            def delayed_replace(*args, **kwargs):
                reached.set()
                if not release.wait(2):
                    raise TimeoutError("test entry write was not released")
                return original(*args, **kwargs)

            try:
                with patch("provider_core.attempt_files.os.replace", side_effect=delayed_replace):
                    task = asyncio.create_task(executor.run(acknowledged))
                    self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    release.set()
                    with self.assertRaises(ExecutionFailed):
                        await executor.settle()
                (retained,) = await store.records()
                self.assertIs(retained.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
                self.assertIsInstance(executor.last_failure.cause, asyncio.CancelledError)
                self.assertEqual(len(executor.last_failure.cleanup_failures), 2)
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                session.retirement_failure = None
                self.assertIsNone(await executor.settle())
                self.assertEqual(len(transport.calls), 5)
                with self.assertRaises(ExecutionRefused):
                    await executor.run(acknowledged)
                claim.release()
            finally:
                release.set()
                await store.close()

    async def test_closed_job_after_entry_write_prevents_execution_and_failure_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            executor = AttemptExecutor(
                api(normal_transport(last_observation=observation(job_state="closed")), store.scope.api_origin),
                store,
                claim,
                session,
                allowed_job_states=frozenset({"open"}), poll_seconds=1,
                task_settle_seconds=0.1,
            )
            try:
                with self.assertRaises(ExecutionFailed) as failed:
                    await executor.run(acknowledged)
                self.assertIsInstance(failed.exception.cause, ExecutionStopped)
                (retained,) = await store.records()
                self.assertIs(retained.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
                self.assertEqual([event[0] for event in session.events], ["prepare", "retire"])
                claim.release()
            finally:
                await store.close()

    async def test_terminal_write_failure_retires_session_but_retains_outcome_and_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            executor = AttemptExecutor(
                api(normal_transport(), store.scope.api_origin), store, claim, session,
                allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1
            )
            original = os.replace
            writes = 0

            def fail_terminal(*args, **kwargs):
                nonlocal writes
                writes += 1
                if writes == 2:
                    raise OSError("private terminal persistence cause")
                return original(*args, **kwargs)

            try:
                with (
                    patch("provider_core.attempt_files.os.replace", side_effect=fail_terminal),
                    self.assertRaises(ExecutionFailed) as failed,
                ):
                    await executor.run(acknowledged)
                self.assertIs(failed.exception.outcome, session.outcome)
                self.assertIsInstance(failed.exception.unconfirmed_record, TerminalPending)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                with self.assertRaises(ExecutionFailed) as settlement:
                    await executor.settle()
                self.assertIs(settlement.exception.cause, failed.exception.cause)
                self.assertIs(settlement.exception.outcome, session.outcome)
            finally:
                await store.close()

    async def test_failed_retirement_preserves_durable_terminal_and_requires_explicit_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            cleanup = RuntimeError("private retirement cause")
            session.retirement_failure = cleanup
            executor = AttemptExecutor(
                api(normal_transport(), store.scope.api_origin), store, claim, session,
                allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1
            )
            try:
                with self.assertRaises(ExecutionFailed) as failed:
                    await executor.run(acknowledged)
                self.assertIs(failed.exception.cause, cleanup)
                self.assertIs(failed.exception.outcome, session.outcome)
                self.assertNotIn("private retirement cause", str(failed.exception))
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                (retained,) = await store.records()
                self.assertIsInstance(retained, TerminalPending)
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                session.retirement_failure = None
                self.assertEqual(await executor.settle(), retained)
                claim.release()
            finally:
                await store.close()

    async def test_cancelled_terminal_write_preserves_result_and_settles_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            executor = AttemptExecutor(
                api(normal_transport(), store.scope.api_origin), store, claim, session,
                allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1
            )
            reached, release = threading.Event(), threading.Event()
            original = os.replace
            writes = 0

            def delay_terminal(*args, **kwargs):
                nonlocal writes
                writes += 1
                if writes == 2:
                    reached.set()
                    if not release.wait(2):
                        raise TimeoutError("test terminal write was not released")
                return original(*args, **kwargs)

            try:
                with patch("provider_core.attempt_files.os.replace", side_effect=delay_terminal):
                    task = asyncio.create_task(executor.run(acknowledged))
                    self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIs(executor.last_failure.outcome, session.outcome)
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    release.set()
                    terminal = await executor.settle()
                self.assertEqual(await store.records(), (terminal,))
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertEqual(writes, 2)
                claim.release()
            finally:
                release.set()
                await store.close()
