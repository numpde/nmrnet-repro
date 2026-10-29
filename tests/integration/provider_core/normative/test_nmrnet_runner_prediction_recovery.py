"""Real runner ownership gates one prediction replacement with frozen inputs.

The child uses the existing framed scientific server and authored outcomes. This
qualifies the new factory/runner connection, not released-model accuracy or OOM.
"""

import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from nmrnet_provider.native_factory import NativeAttemptFactory, NativePolicy
from nmrnet_provider.native_session import NativeOperation
from nmrnet_provider.session import NMRNetSession
from nmrnet_provider.worker_client import PredictionExecutionFailed
from provider_core.controller import LiveStartPolicy
from provider_core.execution import ConditionRecovery, ObservationRecovery
from tests.fakes.execution import unexpected_condition
from tests.fakes.runner import RunningSupervisor
from tests.integration.nmrnet.smiles_ports import SessionPorts


CHILD = r'''
import argparse, asyncio, json, os, socket
from pathlib import Path
from nmrnet_inference.worker import serve_connection
from nmrnet_provider.worker_protocol import send
from tests.integration.nmrnet.test_nmrnet_session import ScientificWorker

parser = argparse.ArgumentParser()
parser.add_argument('--fixture-root', type=Path)
parser.add_argument('--session-fd', type=int)
parser.add_argument('--lease-fd', type=int)
parser.add_argument('--operation')
args = parser.parse_args()
facts = json.loads((args.fixture_root / 'facts.json').read_bytes())
worker = ScientificWorker(facts['artifacts'], facts['runtime'])

class Prediction:
    def prepare(self, requests):
        received = {'operation': args.operation, 'pid': os.getpid(), 'requests': [raw.hex() for raw in requests]}
        (args.fixture_root / (args.operation + '.json')).write_text(json.dumps(received))
        return asyncio.run(worker.prepare(requests))

    def execute(self, handle):
        try:
            descriptor = os.open(args.fixture_root / 'first-execution', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return asyncio.run(worker.execute(handle))
        os.close(descriptor)
        raise RuntimeError('Authored first-child execution failure')

with socket.socket(fileno=args.session_fd) as channel:
    send(channel, {'kind': 'startup', 'allocation': args.operation, 'runtime': facts['runtime']}, 3)
    serve_connection(channel, Prediction(), allocation=args.operation, io_seconds=3)
'''


class RunnerPredictionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_replacement_waits_for_real_retirement_and_reuses_exact_science(self):
        ports = SessionPorts()
        with tempfile.TemporaryDirectory(prefix="prediction-replacement-") as temporary:
            root = Path(temporary)
            child = root / "child.py"
            # The child inherits the lane's installed package and fixture search
            # paths; production uses its immutable image-owned entrypoint.
            child.write_text("import sys\nsys.path[:0] = " + repr(sys.path) + "\n" + CHILD)
            (root / "facts.json").write_text(json.dumps({
                "artifacts": ports.worker.artifacts, "runtime": ports.worker.runtime,
            }))
            runner = RunningSupervisor(role="prediction", worker_argv=[
                sys.executable, str(child), "--fixture-root", str(root),
            ])
            operations = []

            def operation(**arguments):
                owned = NativeOperation(**(arguments | {"endpoint": runner.path}))
                operations.append(owned)
                return owned

            factory = NativeAttemptFactory(
                admission_stopping=asyncio.Event(),
                config=SimpleNamespace(), claims=object(),
                execution_identity={}, selected_artifacts=ports.worker.artifacts,
                policy=NativePolicy(allocation_seconds=3, prediction_seconds=3, source_seconds=1),
                observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
                condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
                start_policy=LiveStartPolicy(cadence_seconds=1, on_retry=Mock(), on_recovered=Mock()),
                on_delivery_retry=Mock(),
                on_prediction_retry=Mock(), on_operator_notice=Mock(), on_unconfirmed_phase_progress=Mock(),
            )
            notice = Mock()

            async def replace():
                if not operations:
                    return await factory.allocate_prediction(job_ref="job:example")
                old, = operations
                self.assertTrue(old.session.retirement.scratch_released)
                self.assertTrue(old.writer.is_closing())
                self.assertTrue(old._retirement.done())
                self.assertIsNone(old._retirement.result())
                self.assertTrue(old._science_closure.done())
                self.assertIsNone(old._science_closure.result())
                self.assertEqual(list(runner.scratch.iterdir()), [])
                return await factory.allocate_prediction(job_ref="job:example")

            session = None
            try:
                with patch("nmrnet_provider.native_factory.NativeOperation", side_effect=operation):
                    session = NMRNetSession(
                        allocate_prediction=replace, execution_identity={"image_id": "fixture"},
                        selected_artifacts=ports.worker.artifacts,
                        on_prediction_retry=notice, input_preparation=ports.preparation,
                    )
                    prepared = await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
                    frozen_requests = [raw.hex() for raw in session._prepared.requests]
                    outcome = await session.execute(prepared, report_condition=unexpected_condition)
                self.assertEqual(len(operations), 2)
                first, second = (json.loads((root / (owner.allocation + ".json")).read_bytes()) for owner in operations)
                self.assertNotEqual(first["operation"], second["operation"])
                self.assertEqual(first["requests"], frozen_requests)
                self.assertEqual(second["requests"], frozen_requests)
                self.assertEqual(len(first["requests"]), 2)
                self.assertIsInstance(session.prediction_failure, PredictionExecutionFailed)
                self.assertIn("Authored first-child execution failure", session.prediction_failure.diagnostic)
                notice.assert_called_once_with(ports.acknowledged, session.prediction_failure, operations[0])
                self.assertEqual(json.loads(outcome.content)["geometries"][0]["predictions"][0]["shift_ppm"], 1.0)
                self.assertEqual(factory.allocations, [])
                self.assertEqual(factory.clients, {})
            finally:
                # Every retained owner gets its own cleanup attempt, including
                # when an earlier retirement raises. ExitStack preserves causes.
                async with AsyncExitStack() as cleanup:
                    cleanup.callback(runner.close)
                    for owner in operations:
                        cleanup.push_async_callback(owner.settle)
                        cleanup.push_async_callback(owner.retire)
                    if session is not None:
                        cleanup.push_async_callback(session.retire)
