"""Unconfirmed phase delivery needs a fresh lifecycle read, never a PUT replay."""

import json
import tempfile
import unittest

from provider_core.attempt_record import LocalExecutionPhase, TerminalOperation
from provider_core.execution import AttemptExecutor
from tests.fakes.execution import Session, acknowledged_store
from tests.fakes.phase_progress import PhaseProgressTransport
from tests.fakes.publication import api


class PhaseProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_phase_can_continue_after_retirement_notice_and_fresh_allowed_read(self):
        for phase in ("preparing", "running"):
            with self.subTest(phase=phase):
                await self.check_phase(phase)

    async def check_phase(self, phase):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            transport = PhaseProgressTransport(phase)
            notices = []

            def unconfirmed(attempt, reported_phase, failure):
                self.assertIs(attempt, acknowledged)
                self.assertEqual(reported_phase, phase)
                self.assertIs(failure, client.last_failure)
                self.assertEqual(failure.status, 503)
                self.assertTrue(failure.attempt_mutation_may_have_committed)
                self.assertTrue(transport.retirement_confirmed)
                self.assertEqual(transport.fresh_reads, 0)
                expected_events = [] if phase == "preparing" else ["prepare"]
                self.assertEqual([event[0] for event in session.events], expected_events)
                notices.append(failure)

            client = api(transport, store.scope.api_origin)
            executor = AttemptExecutor(
                client,
                store,
                claim,
                session,
                allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                on_unconfirmed_phase_progress=unconfirmed,
            )
            try:
                terminal = await executor.run(acknowledged)
                self.assertEqual(len(notices), 1)
                self.assertEqual(transport.fresh_reads, 1)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertIs(session.events[1][1].local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
                self.assertIs(terminal.operation, TerminalOperation.COMPLETE)
                self.assertEqual(terminal.evidence, session.outcome.evidence)
                self.assertEqual(await store.records(), (terminal,))
                commands = [json.loads(request.content) for request, _ in transport.calls if request.method == "PUT"]
                self.assertEqual(
                    [(item["phase"], item["condition_code"]) for item in commands], [("preparing", None), ("running", None)]
                )
                self.assertIsNone(executor.last_failure)
                claim.release()
            finally:
                await store.close()

    async def test_invalid_observer_refuses_before_session_or_api_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            store, _acknowledged, claim = await acknowledged_store(directory)
            transport = PhaseProgressTransport("preparing")
            session = Session(store)
            try:
                with self.assertRaises(ValueError):
                    AttemptExecutor(
                        api(transport, store.scope.api_origin),
                        store,
                        claim,
                        session,
                        allowed_job_states=frozenset({"open"}),
                        poll_seconds=1,
                        task_settle_seconds=0.1,
                        on_unconfirmed_phase_progress=False,
                    )
                self.assertEqual(transport.calls, [])
                self.assertEqual(session.events, [])
                claim.release()
            finally:
                await store.close()
