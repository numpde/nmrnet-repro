"""An interrupted prediction does not trigger another scientific execution."""

import asyncio
import hashlib
import json
import unittest
from unittest.mock import AsyncMock, patch

from tests.fakes.execution import unexpected_condition
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.session import NMRNetSession
from nmrnet_provider.worker_client import PredictionExecutionFailed
from provider_core.analysis import AnalysisFailure


class PredictionFailureTests(unittest.IsolatedAsyncioTestCase):
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
        for failure in (PredictionExecutionFailed("Prediction failed"),
                        asyncio.IncompleteReadError(b"", 1), TimeoutError("Prediction timed out")):
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
