"""Prediction recovery preserves prepared science within the original Attempt."""

import json
import unittest
from unittest.mock import AsyncMock, Mock

from tests.fakes.execution import unexpected_condition
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.session import NMRNetSession, PredictionRecovery
from nmrnet_provider.worker_client import PredictionExecutionFailed


class PredictionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_replacement_reuses_frozen_requests_and_retains_first_fault(self):
        ports = SessionPorts()
        first = PredictionExecutionFailed("Authored allocator failure")
        replacement = type(ports.worker)(ports.worker.artifacts, ports.worker.runtime)
        host = type(ports.host)()
        allocate = AsyncMock(return_value=(replacement, host, replacement.runtime))
        notice = Mock()
        session = NMRNetSession(
            worker=ports.worker,
            retirement=ports.host,
            execution_identity={"image_id": "fixture"},
            selected_artifacts=ports.worker.artifacts,
            runtime_expectations=ports.worker.runtime,
            prediction_recovery=PredictionRecovery(allocate, notice),
            input_preparation=ports.preparation,
        )
        ports.worker.execute = AsyncMock(side_effect=first)
        prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        outcome = await session.execute(prepared, report_condition=unexpected_condition)
        self.assertTrue(ports.host.confirmed)
        self.assertEqual(replacement.requests, ports.worker.requests)
        self.assertEqual(replacement.events, ["prepare", "execute"])
        allocate.assert_awaited_once_with()
        notice.assert_called_once_with(ports.acknowledged, first, ports.host)
        self.assertIs(session.prediction_failure, first)
        self.assertEqual(json.loads(outcome.content)["geometries"][0]["predictions"][0]["shift_ppm"], 1.0)
        await session.retire()
        self.assertTrue(host.confirmed)


if __name__ == "__main__":
    unittest.main()
