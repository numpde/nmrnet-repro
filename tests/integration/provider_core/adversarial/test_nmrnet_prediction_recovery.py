"""Replacement never bypasses retirement, admission or the two-execution bound."""

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from tests.fakes.execution import unexpected_condition
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.session import NMRNetSession, PredictionRecovery, PredictionRecoveryFailed, SessionRetirementFailed
from nmrnet_provider.worker_client import PredictionExecutionFailed, WorkerReportedFailure
from nmrnet_provider.worker_protocol import WorkerProtocolError


class PredictionRecoveryRefusalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ports = SessionPorts()
        self.replacement = type(self.ports.worker)(self.ports.worker.artifacts, self.ports.worker.runtime)
        self.host = type(self.ports.host)()
        self.allocate = AsyncMock(return_value=(self.replacement, self.host, self.replacement.runtime))
        self.notice = Mock()
        self.first = PredictionExecutionFailed("Authored first prediction failure")
        self.ports.worker.execute = AsyncMock(side_effect=self.first)
        self.session = NMRNetSession(
            worker=self.ports.worker,
            retirement=self.ports.host,
            execution_identity={"image_id": "fixture"},
            selected_artifacts=self.ports.worker.artifacts,
            runtime_expectations=self.ports.worker.runtime,
            input_preparation=self.ports.preparation,
            prediction_recovery=PredictionRecovery(self.allocate, self.notice),
        )

    async def execute(self):
        prepared = await self.session.prepare(
            self.ports.acknowledged, self.ports.instructions, report_condition=unexpected_condition
        )
        return await self.session.execute(prepared, report_condition=unexpected_condition)

    async def test_generic_refusal_protocol_foreign_timeout_and_cancellation_never_replace(self):
        for failure in (
            WorkerReportedFailure("refused"),
            WorkerProtocolError("bad reply"),
            TimeoutError("independent"),
            asyncio.CancelledError(),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.setUp()
                self.ports.worker.execute.side_effect = failure
                with self.assertRaises(type(failure)) as caught:
                    await self.execute()
                self.assertIs(caught.exception, failure)
                self.allocate.assert_not_called()
                self.notice.assert_not_called()
                await self.session.retire()

    async def test_prepare_refusal_does_not_enter_execution_or_replace(self):
        failure = PredictionExecutionFailed("Unexpected execution outcome during prepare")
        self.ports.worker.prepare = AsyncMock(side_effect=failure)
        with self.assertRaises(PredictionExecutionFailed):
            await self.execute()
        self.ports.worker.execute.assert_not_called()
        self.allocate.assert_not_called()
        await self.session.retire()

    async def test_second_failure_is_terminal_and_keeps_both_causes(self):
        second = asyncio.IncompleteReadError(b"PRIVATE reply prefix", 4)
        self.replacement.execute = AsyncMock(side_effect=second)
        with self.assertRaises(PredictionRecoveryFailed) as caught:
            await self.execute()
        self.assertIs(caught.exception.first_failure, self.first)
        self.assertIs(caught.exception.cause, second)
        self.allocate.assert_awaited_once_with()
        self.replacement.execute.assert_awaited_once()
        await self.session.retire()

    async def test_host_and_channel_observation_independently_gate_replacement(self):
        for owner in ("host", "channel"):
            with self.subTest(owner=owner):
                await self.check_retirement_barrier(owner)

    async def check_retirement_barrier(self, owner):
        self.setUp()
        entered, release = asyncio.Event(), asyncio.Event()

        async def held():
            entered.set()
            await release.wait()

        if owner == "host":
            self.ports.host.retire = held
        else:
            self.ports.worker.wait_closed = held
        task = asyncio.create_task(self.execute())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.allocate.assert_not_called()
            release.set()
            await asyncio.wait_for(task, 2)
            self.allocate.assert_awaited_once_with()
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await self.session.retire()

    async def test_cleanup_failure_blocks_replacement_and_keeps_initial_fault(self):
        cleanup = OSError(5, "Authored independent retirement failure")
        self.ports.host.failure = cleanup
        with self.assertRaises(PredictionRecoveryFailed) as caught:
            await self.execute()
        self.assertIs(caught.exception.first_failure, self.first)
        self.assertIs(type(caught.exception.cause), SessionRetirementFailed)
        self.assertIn(("native_retirement", cleanup), caught.exception.cause.failures)
        self.allocate.assert_not_called()
        self.ports.host.failure = None
        await self.session.retire()

    async def test_cancellation_during_old_worker_retirement_remains_cancellation(self):
        interruption = asyncio.CancelledError()
        self.ports.host.retire = AsyncMock(side_effect=interruption)
        with self.assertRaises(asyncio.CancelledError) as caught:
            await self.execute()
        self.assertIs(caught.exception, interruption)
        self.assertIs(caught.exception.__cause__.first_failure, self.first)
        self.allocate.assert_not_called()
        self.ports.host.retire.side_effect = None
        await self.session.retire()

    async def test_runtime_drift_blocks_replacement_prepare_but_keeps_new_allocation_owned(self):
        self.allocate.return_value = self.replacement, self.host, {"python": "changed"}
        with self.assertRaises(PredictionRecoveryFailed) as caught:
            await self.execute()
        self.assertIs(type(caught.exception.cause), ValueError)
        self.assertEqual(self.replacement.events, [])
        await self.session.retire()
        self.assertTrue(self.host.confirmed)

    async def test_retirement_waits_for_pending_handoff_and_cleans_late_return(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def allocate():
            entered.set()
            await release.wait()
            return self.replacement, self.host, self.replacement.runtime

        self.allocate.side_effect = allocate
        task = asyncio.create_task(self.execute())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with self.assertRaises(SessionRetirementFailed):
                await self.session.retire()
            release.set()
            with self.assertRaises(PredictionRecoveryFailed):
                await asyncio.wait_for(task, 2)
            self.assertEqual(self.replacement.events, [])
            await self.session.retire()
            self.assertTrue(self.host.confirmed)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await self.session.retire()

    async def test_cancellation_during_replacement_preserves_first_and_original_interruption(self):
        entered = asyncio.Event()

        async def allocate():
            entered.set()
            await asyncio.Event().wait()

        self.allocate.side_effect = allocate
        task = asyncio.create_task(self.execute())
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertIs(type(caught.exception.__cause__), PredictionRecoveryFailed)
        self.assertIs(caught.exception.__cause__.first_failure, self.first)
        self.allocate.assert_awaited_once_with()
        await self.session.retire()

    async def test_handoff_during_old_channel_retirement_cannot_confirm_the_new_pair(self):
        allocating, release_allocation = asyncio.Event(), asyncio.Event()
        retiring, release_retirement = asyncio.Event(), asyncio.Event()

        async def allocate():
            allocating.set()
            await release_allocation.wait()
            return self.replacement, self.host, self.replacement.runtime

        original = self.ports.host.retire_channel

        async def held_retirement(worker):
            retiring.set()
            await release_retirement.wait()
            return await original(worker)

        self.allocate.side_effect = allocate
        task = asyncio.create_task(self.execute())
        retirement = None
        try:
            await asyncio.wait_for(allocating.wait(), 2)
            self.ports.host.retire_channel = held_retirement
            retirement = asyncio.create_task(self.session.retire())
            await asyncio.wait_for(retiring.wait(), 2)
            release_allocation.set()
            with self.assertRaises(PredictionRecoveryFailed):
                await asyncio.wait_for(task, 2)
            release_retirement.set()
            with self.assertRaises(SessionRetirementFailed):
                await asyncio.wait_for(retirement, 2)
            self.assertFalse(self.host.confirmed)
            await self.session.retire()
            self.assertTrue(self.host.confirmed)
        finally:
            release_allocation.set()
            release_retirement.set()
            await asyncio.gather(task, *(() if retirement is None else (retirement,)), return_exceptions=True)
            await self.session.retire()


if __name__ == "__main__":
    unittest.main()
