"""Live observation during entry persistence preserves fresh-read-before-execute."""

import asyncio
import tempfile
import unittest
from unittest.mock import patch

from provider_core.attempt_record import TerminalPending
from provider_core.execution import AttemptExecutor
from tests.fakes.api import Transport, reply
from tests.fakes.attempts import observation, progress_response
from tests.fakes.entry_write import EntryWriteBarrier
from tests.fakes.execution import Session, acknowledged_store
from tests.fakes.job_input import response
from tests.fakes.publication import api


class EntryPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_poll_then_completed_original_write_still_checks_state_before_one_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            polled = asyncio.Event()
            release_poll = asyncio.Event()

            class EntryTransport(Transport):
                async def exchange(self, request, *, maximum_bytes, owner=None):
                    if len(self.calls) == 5:
                        polled.set()
                        await release_poll.wait()
                    return await super().exchange(request, maximum_bytes=maximum_bytes, owner=owner)

            transport = EntryTransport(reply(observation()), reply(response()), reply(progress_response()),
                                       reply(observation()), reply(progress_response(phase="running")),
                                       reply(observation()), reply(observation()), reply(observation()))
            executor = AttemptExecutor(api(transport, store.scope.api_origin), store, claim, session,
                                       allowed_job_states=frozenset({"open"}), poll_seconds=0.01, task_settle_seconds=0.05)
            barrier = EntryWriteBarrier()
            task = None
            try:
                with patch("provider_core.attempt_files.os.replace", side_effect=barrier.replace):
                    task = asyncio.create_task(executor.run(acknowledged))
                    self.assertTrue(await asyncio.to_thread(barrier.reached.wait, 1))
                    await asyncio.wait_for(polled.wait(), 1)
                    self.assertEqual([event[0] for event in session.events], ["prepare"])
                    barrier.release.set()
                    release_poll.set()
                    terminal = await task
                self.assertIsInstance(terminal, TerminalPending)
                self.assertEqual(await store.records(), (terminal,))
                self.assertEqual(barrier.calls, 2)
                self.assertEqual(len(transport.calls), 8)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                claim.release()
            finally:
                barrier.release.set()
                release_poll.set()
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if store.pending_owner is executor:
                    await executor.settle()
                await store.close()
