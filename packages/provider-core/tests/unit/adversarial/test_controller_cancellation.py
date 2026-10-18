"""Late inventory/admission persistence never resumes a cancelled controller."""

import asyncio
import tempfile
import unittest
from contextlib import AsyncExitStack
from unittest.mock import patch

from provider_core.attempt_record import AcknowledgedAttempt, LocalExecutionPhase
from provider_core.controller import AttemptController, ControllerRefused
from provider_core.ownership import AttemptClaims
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import start_response
from tests.fakes.controller import SingleRetirementSession, StoreCallBarrier, cancel_task, empty_store
from tests.fakes.entry_write import EntryWriteBarrier
from tests.fakes.execution import execute_with_watchdog
from tests.fakes.publication import api


class ControllerCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_inventory_read_remains_owned_and_late_read_never_starts(self):
        async with AsyncExitStack() as cleanup:
            directory = cleanup.enter_context(tempfile.TemporaryDirectory())
            store = await empty_store(directory, maximum_records=1)
            cleanup.push_async_callback(store.close)
            transport = Transport()
            session = SingleRetirementSession(store)
            controller = AttemptController(api(transport, scope().api_origin), store, AttemptClaims(), session,
                                           allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            cleanup.push_async_callback(controller.settle)
            barrier = StoreCallBarrier(store._files.records)
            cleanup.callback(barrier.release.set)
            with patch.object(store._files, "records", barrier.call):
                task = asyncio.create_task(execute_with_watchdog(controller, record()))
                cleanup.push_async_callback(cancel_task, task)
                self.assertTrue(await asyncio.to_thread(barrier.reached.wait, 1))
                with self.assertRaises(ControllerRefused):
                    await controller.settle()
                with self.assertRaises(ControllerRefused):
                    await controller.run(record())
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIs(store.pending_owner, controller)
                self.assertIsInstance(controller.last_failure.cause, asyncio.CancelledError)
                self.assertEqual([event[0] for event in session.events], ["retire"])
                barrier.release.set()
                self.assertIsNone(await controller.settle())
            self.assertEqual(transport.calls, [])
            self.assertEqual(await store.records(), ())

    async def test_cancelled_acknowledgement_write_settles_to_original_admission_without_science(self):
        async with AsyncExitStack() as cleanup:
            directory = cleanup.enter_context(tempfile.TemporaryDirectory())
            store = await empty_store(directory, maximum_records=1)
            cleanup.push_async_callback(store.close)
            transport = Transport(reply(start_response()))
            session = SingleRetirementSession(store)
            controller = AttemptController(api(transport, scope().api_origin), store, AttemptClaims(), session,
                                           allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=0.1)
            cleanup.push_async_callback(controller.settle)
            # Admission uses a no-replace link. Its first rename is precisely the
            # acknowledgement write, so the existing real rename barrier applies.
            barrier = EntryWriteBarrier()
            cleanup.callback(barrier.release.set)
            with patch("os.replace", barrier.replace):
                task = asyncio.create_task(execute_with_watchdog(controller, record()))
                cleanup.push_async_callback(cancel_task, task)
                self.assertTrue(await asyncio.to_thread(barrier.reached.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertIs(store.pending_owner, controller.operation)
                self.assertIsInstance(controller.last_failure.cause, asyncio.CancelledError)
                barrier.release.set()
                acknowledged = await controller.settle()
            self.assertIsInstance(acknowledged, AcknowledgedAttempt)
            self.assertIs(acknowledged.local_phase, LocalExecutionPhase.NOT_ENTERED)
            self.assertEqual(await store.records(), (acknowledged,))
            self.assertEqual([event[0] for event in session.events], ["retire"])
            self.assertEqual(len(transport.calls), 1)
            with self.assertRaises(ControllerRefused):
                await controller.run(record())
