"""An interrupted prediction does not trigger another scientific execution."""

import asyncio
import hashlib
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

from tests.fakes.execution import unexpected_condition
from tests.integration.nmrnet.smiles_ports import SessionPorts
from tests.integration.nmrnet.test_nmrnet_session import HostOwner, ScientificWorker

from nmrnet_provider.session import NMRNetSession
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.worker_client import ModelCallFailed, PredictionExecutionFailed, WorkerExchangeTimedOut
from provider_core.analysis import AnalysisFailure, AnalysisOutcomeUnresolved, AnalysisSuccess


class PredictionFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_call_failure_retires_original_before_one_frozen_batch_retry(self):
        ports = SessionPorts()
        original_worker, original_host = ports.worker, ports.host
        replacement = ScientificWorker(original_worker.artifacts, original_worker.runtime)
        replacement_host = HostOwner()
        original_worker.execute = AsyncMock(side_effect=ModelCallFailed("private first failure"))
        notices = []

        async def allocate():
            if not original_host.confirmed:
                return original_worker, original_host, original_worker.runtime
            return replacement, replacement_host, replacement.runtime

        session = NMRNetSession(
            allocate_prediction=allocate, execution_identity={"image_id": "fixture"},
            selected_artifacts=original_worker.artifacts, input_preparation=ports.preparation,
            max_retries=1, prediction_seconds=5,
            on_prediction_failure=lambda failure, attempt: notices.append((failure, attempt)),
        )
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        outcome = await session.execute(prepared, report_condition=unexpected_condition)
        self.assertIsInstance(outcome, AnalysisSuccess)
        self.assertTrue(original_host.confirmed)
        self.assertEqual(original_worker.requests, replacement.requests)
        self.assertEqual(replacement.events.count("execute"), 1)
        self.assertEqual(len(notices), 1)
        self.assertIs(notices[0][1], ports.acknowledged)
        await session.retire()
        self.assertTrue(replacement_host.confirmed)

    async def test_lost_replacement_reply_remains_unknown_without_third_execution(self):
        ports = SessionPorts()
        first = ModelCallFailed("private first failure")
        ports.worker.execute = AsyncMock(side_effect=first)
        replacement = ScientificWorker(ports.worker.artifacts, ports.worker.runtime)
        lost = asyncio.IncompleteReadError(b"", 1)
        replacement.execute = AsyncMock(side_effect=lost)
        replacement_host = HostOwner()

        async def allocate():
            if not ports.host.confirmed:
                return ports.worker, ports.host, ports.worker.runtime
            return replacement, replacement_host, replacement.runtime

        allocation = AsyncMock(side_effect=allocate)
        session = NMRNetSession(
            allocate_prediction=allocation, execution_identity={"image_id": "fixture"},
            selected_artifacts=ports.worker.artifacts, input_preparation=ports.preparation,
            max_retries=1, prediction_seconds=5,
        )
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        with self.assertRaises(AnalysisOutcomeUnresolved) as caught:
            await session.execute(prepared, report_condition=unexpected_condition)
        self.assertIs(caught.exception.__cause__, lost)
        self.assertIs(lost.__context__, first)
        self.assertEqual(allocation.await_count, 2)
        replacement.execute.assert_awaited_once()
        await session.retire()
        self.assertTrue(replacement_host.confirmed)

    async def test_changed_replacement_runtime_blocks_second_execution(self):
        ports = SessionPorts()
        first = ModelCallFailed("private first failure")
        ports.worker.execute = AsyncMock(side_effect=first)
        replacement = ScientificWorker(ports.worker.artifacts, ports.worker.runtime)
        replacement.execute = AsyncMock()
        replacement_host = HostOwner()

        async def allocate():
            if not ports.host.confirmed:
                return ports.worker, ports.host, ports.worker.runtime
            return replacement, replacement_host, {"python": "different"}

        session = NMRNetSession(
            allocate_prediction=allocate, execution_identity={"image_id": "fixture"},
            selected_artifacts=ports.worker.artifacts, input_preparation=ports.preparation,
            max_retries=1, prediction_seconds=5,
        )
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        with self.assertRaisesRegex(ValueError, "replacement runtime") as caught:
            await session.execute(prepared, report_condition=unexpected_condition)
        self.assertIs(caught.exception.__cause__, first)
        self.assertEqual(replacement.events, [])
        replacement.execute.assert_not_awaited()
        await session.retire()
        self.assertTrue(replacement_host.confirmed)

    async def test_retry_budget_expires_during_retirement_without_replacement(self):
        ports = SessionPorts()
        original = ModelCallFailed("private first failure")
        ports.worker.execute = AsyncMock(side_effect=original)
        notices = []
        allocate = AsyncMock(side_effect=ports.allocate_prediction)
        retire = ports.host.retire_channel

        async def slow_retirement(worker):
            await asyncio.sleep(0.04)
            return await retire(worker)

        session = NMRNetSession(
            allocate_prediction=allocate, execution_identity={"image_id": "fixture"},
            selected_artifacts=ports.worker.artifacts, input_preparation=ports.preparation,
            max_retries=1, prediction_seconds=0.01,
            on_prediction_failure=lambda failure, attempt: notices.append(failure),
        )
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        with patch.object(ports.host, "retire_channel", side_effect=slow_retirement):
            outcome = await session.execute(prepared, report_condition=unexpected_condition)
        self.assertIsInstance(outcome, AnalysisFailure)
        self.assertTrue(ports.host.confirmed)
        allocate.assert_awaited_once()
        self.assertEqual(notices, [original])
        self.assertIn("retirement", json.loads(outcome.evidence.content)["prediction_failure"]["retry_not_started"])
        await session.retire()

    async def test_other_prediction_failure_does_not_authorize_retry(self):
        ports = SessionPorts()
        ports.worker.execute = AsyncMock(side_effect=PredictionExecutionFailed("private failure"))
        allocate = AsyncMock(side_effect=ports.allocate_prediction)
        session = NMRNetSession(
            allocate_prediction=allocate, execution_identity={"image_id": "fixture"},
            selected_artifacts=ports.worker.artifacts, input_preparation=ports.preparation,
            max_retries=1, prediction_seconds=5,
        )
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        outcome = await session.execute(prepared, report_condition=unexpected_condition)
        self.assertIsInstance(outcome, AnalysisFailure)
        allocate.assert_awaited_once()
        await session.retire()

    async def test_failed_retirement_preserves_first_failure_and_blocks_replacement(self):
        ports = SessionPorts()
        original = ModelCallFailed("private first failure")
        cleanup = OSError("private retirement failure")
        ports.worker.execute = AsyncMock(side_effect=original)
        ports.host.failure = cleanup
        allocate = AsyncMock(side_effect=ports.allocate_prediction)
        session = NMRNetSession(
            allocate_prediction=allocate, execution_identity={"image_id": "fixture"},
            selected_artifacts=ports.worker.artifacts, input_preparation=ports.preparation,
            max_retries=1, prediction_seconds=5,
        )
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        with self.assertRaises(ExceptionGroup) as caught:
            await session.execute(prepared, report_condition=unexpected_condition)
        self.assertIs(caught.exception.exceptions[0], original)
        self.assertIs(caught.exception.exceptions[1].cause, cleanup)
        evidence = json.dumps(failure_evidence(caught.exception, secrets=("private",)))
        self.assertIn("ModelCallFailed", evidence)
        self.assertIn("OSError", evidence)
        self.assertNotIn("private first failure", evidence)
        self.assertNotIn("private retirement failure", evidence)
        allocate.assert_awaited_once()
        ports.host.failure = None
        await session.retire()

    async def test_stop_during_retry_retirement_never_allocates_replacement(self):
        ports = SessionPorts()
        original = ModelCallFailed("private first failure")
        ports.worker.execute = AsyncMock(side_effect=original)
        allocate = AsyncMock(side_effect=ports.allocate_prediction)
        entered, release = asyncio.Event(), asyncio.Event()
        retire = ports.host.retire_channel

        async def held_retirement(worker):
            entered.set()
            await release.wait()
            return await retire(worker)

        session = NMRNetSession(
            allocate_prediction=allocate, execution_identity={"image_id": "fixture"},
            selected_artifacts=ports.worker.artifacts, input_preparation=ports.preparation,
            max_retries=1, prediction_seconds=5,
        )
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        with patch.object(ports.host, "retire_channel", side_effect=held_retirement) as retirement:
            execution = asyncio.create_task(session.execute(prepared, report_condition=unexpected_condition))
            await entered.wait()
            execution.cancel()
            with self.assertRaises(asyncio.CancelledError) as cancelled:
                await execution
            self.assertIs(cancelled.exception.__context__, original)
            shutdown = asyncio.create_task(session.retire())
            release.set()
            await shutdown
        allocate.assert_awaited_once()
        retirement.assert_awaited_once()
        self.assertTrue(ports.host.confirmed)


    async def test_retry_budget_expires_in_preparation_without_second_execution(self):
        ports = SessionPorts()
        original = ModelCallFailed("private first failure")
        ports.worker.execute = AsyncMock(side_effect=original)
        replacement = ScientificWorker(ports.worker.artifacts, ports.worker.runtime)

        async def blocked_prepare(_requests):
            await asyncio.Event().wait()

        replacement.prepare = AsyncMock(side_effect=blocked_prepare)
        replacement.execute = AsyncMock()
        replacement_host = HostOwner()
        notices = []

        async def allocate():
            if not ports.host.confirmed:
                return ports.worker, ports.host, ports.worker.runtime
            return replacement, replacement_host, replacement.runtime

        session = NMRNetSession(
            allocate_prediction=allocate, execution_identity={"image_id": "fixture"},
            selected_artifacts=ports.worker.artifacts, input_preparation=ports.preparation,
            max_retries=1, prediction_seconds=0.02,
            on_prediction_failure=lambda failure, attempt: notices.append(failure),
        )
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        outcome = await session.execute(prepared, report_condition=unexpected_condition)
        self.assertIsInstance(outcome, AnalysisFailure)
        replacement.execute.assert_not_awaited()
        self.assertEqual(notices, [original])
        self.assertIn("preparation", json.loads(outcome.evidence.content)["prediction_failure"]["retry_not_started"])
        await session.retire()

    async def test_retry_budget_expires_during_notice_without_second_execution(self):
        ports = SessionPorts()
        original = ModelCallFailed("private first failure")
        ports.worker.execute = AsyncMock(side_effect=original)
        replacement = ScientificWorker(ports.worker.artifacts, ports.worker.runtime)
        replacement.execute = AsyncMock()
        replacement_host = HostOwner()

        async def allocate():
            if not ports.host.confirmed:
                return ports.worker, ports.host, ports.worker.runtime
            return replacement, replacement_host, replacement.runtime

        session = NMRNetSession(
            allocate_prediction=allocate, execution_identity={"image_id": "fixture"},
            selected_artifacts=ports.worker.artifacts, input_preparation=ports.preparation,
            max_retries=1, prediction_seconds=0.02,
            on_prediction_failure=lambda failure, attempt: time.sleep(0.05),
        )
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        outcome = await session.execute(prepared, report_condition=unexpected_condition)
        self.assertIsInstance(outcome, AnalysisFailure)
        replacement.execute.assert_not_awaited()
        self.assertIn("before retry dispatch", json.loads(outcome.evidence.content)["prediction_failure"]["retry_not_started"])
        await session.retire()

    async def test_large_diagnostic_does_not_prevent_truthful_failure(self):
        ports = SessionPorts()
        original = PredictionExecutionFailed("💧" * 300000)
        ports.worker.execute = AsyncMock(side_effect=original)
        prepared = await ports.session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        outcome = await ports.session.execute(prepared, report_condition=unexpected_condition)
        diagnostic = json.dumps(original.diagnostic, ensure_ascii=True).encode("ascii")
        evidence = json.loads(outcome.evidence.content)["prediction_failure"]
        self.assertEqual(evidence["diagnostic_bytes"], len(diagnostic))
        self.assertEqual(evidence["diagnostic_sha256"], hashlib.sha256(diagnostic).hexdigest())
        self.assertIsInstance(outcome, AnalysisFailure)
        await ports.session.retire()

    async def test_failure_after_retirement_is_not_selected_as_outcome(self):
        ports = SessionPorts()
        session = ports.session
        entered, release = asyncio.Event(), asyncio.Event()
        original = PredictionExecutionFailed("private diagnostic")

        async def late_failure(_handle):
            entered.set()
            await release.wait()
            raise original

        ports.worker.execute = AsyncMock(side_effect=late_failure)
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        task = asyncio.create_task(session.execute(prepared, report_condition=unexpected_condition))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await session.retire()
            release.set()
            with self.assertRaisesRegex(ValueError, "after retirement began") as caught:
                await task
            self.assertIs(caught.exception.__cause__, original)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    async def test_evidence_failure_keeps_original_prediction_failure(self):
        ports = SessionPorts()
        session = NMRNetSession(
            allocate_prediction=ports.allocate_prediction,
            execution_identity={"image_id": "fixture"},
            selected_artifacts=ports.worker.artifacts,
            input_preparation=ports.preparation,
        )
        original = PredictionExecutionFailed("private diagnostic")
        ports.worker.execute = AsyncMock(side_effect=original)
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        with patch("nmrnet_provider.session._evidence", side_effect=ValueError("evidence unavailable")):
            with self.assertRaisesRegex(ValueError, "evidence unavailable") as caught:
                await session.execute(prepared, report_condition=unexpected_condition)
        self.assertIs(caught.exception.__cause__, original)
        await session.retire()

    async def test_failure_is_preserved_without_reallocating_or_reexecuting(self):
        interrupted = asyncio.CancelledError()
        interrupted.__cause__ = OSError("independent interruption evidence")
        underlying = TimeoutError()
        underlying.__cause__ = interrupted
        timeout_with_interruption = WorkerExchangeTimedOut("Prediction timed out", send_started=True)
        timeout_with_interruption.__cause__ = underlying
        for failure in (PredictionExecutionFailed("Prediction failed"), ModelCallFailed("Model call failed"),
                        asyncio.IncompleteReadError(b"", 1), TimeoutError("Prediction timed out"),
                        WorkerExchangeTimedOut("Prediction was not sent", send_started=False), timeout_with_interruption):
            with self.subTest(failure=type(failure).__name__):
                ports = SessionPorts()
                allocate = AsyncMock(side_effect=ports.allocate_prediction)
                session = NMRNetSession(
                    allocate_prediction=allocate,
                    execution_identity={"image_id": "fixture"},
                    selected_artifacts=ports.worker.artifacts,
                    input_preparation=ports.preparation,
                )
                ports.worker.execute = AsyncMock(side_effect=failure)
                prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
                if isinstance(failure, PredictionExecutionFailed):
                    outcome = await session.execute(prepared, report_condition=unexpected_condition)
                    self.assertIsInstance(outcome, AnalysisFailure)
                    self.assertEqual(outcome.message, "Prediction did not return a complete verified result.")
                    self.assertNotIn(failure.diagnostic, outcome.message)
                    self.assertNotIn(failure.diagnostic.encode(), outcome.evidence.content)
                elif type(failure) is asyncio.IncompleteReadError:
                    with self.assertRaises(AnalysisOutcomeUnresolved) as caught:
                        await session.execute(prepared, report_condition=unexpected_condition)
                    self.assertIs(caught.exception.__cause__, failure)
                else:
                    with self.assertRaises(type(failure)) as caught:
                        await session.execute(prepared, report_condition=unexpected_condition)
                    self.assertIs(caught.exception, failure)
                allocate.assert_awaited_once()
                ports.worker.execute.assert_awaited_once()
                await session.retire()
                self.assertTrue(ports.host.confirmed)


if __name__ == "__main__":
    unittest.main()
