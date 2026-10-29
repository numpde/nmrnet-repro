"""A real native crash reaches FAIL without prediction replay or lost cleanup.

Scientific preparation and API replies are authored fixtures. The child process,
framed channels, NMRNet session, SDK controller and exit observation are real.
"""

import asyncio
from dataclasses import replace
from functools import partial
import json
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.native_factory import NativeAttemptFactory, NativePolicy
from nmrnet_provider.native_session import NativeOperation, NativeWorkerExited
from nmrnet_provider.session import NMRNetSession
from provider_core.controller import AttemptController, LiveStartPolicy
from provider_core.execution import ConditionRecovery, ObservationRecovery
from provider_core.ownership import AttemptClaims
from provider_core.producer_checkpoint import DeliveryAcceptedCheckpoint
from tests.fakes.api import Transport, api, reply
from tests.fakes.attempts import observation, progress_response, start_response
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.job_input import facts, response
from provider_core.job_input import verify_job_input
from tests.fakes.runner import RunningSupervisor
from tests.integration.nmrnet.smiles_ports import SessionPorts


PARTIAL_REPLY_CHILD = r'''
import argparse, socket
parser = argparse.ArgumentParser()
parser.add_argument('--session-fd', type=int)
parser.add_argument('--lease-fd', type=int)
parser.add_argument('--operation')
args = parser.parse_args()
with socket.socket(fileno=args.session_fd) as channel:
    def received(size):
        result = b''
        while len(result) < size:
            part = channel.recv(size - len(result))
            if not part:
                raise SystemExit(29)
            result += part
        return result
    received(int.from_bytes(received(4), 'big'))
    channel.sendall(b'\x00\x00')
    raise SystemExit(17)
'''


class NativeCrashTests(unittest.IsolatedAsyncioTestCase):
    def factory(self, ports, claims, retry):
        return NativeAttemptFactory(
            admission_stopping=asyncio.Event(),
            config=SimpleNamespace(), claims=claims,
            execution_identity={"image_id": "fixture"}, selected_artifacts=ports.worker.artifacts,
            policy=NativePolicy(allocation_seconds=2, prediction_seconds=2, source_seconds=2),
            observation_recovery=ObservationRecovery(2, 1, Mock(), Mock()),
            condition_recovery=ConditionRecovery(2, 1, Mock(), Mock()),
            start_policy=LiveStartPolicy(cadence_seconds=1, on_retry=Mock(), on_recovered=Mock()),
            on_prediction_retry=retry, on_operator_notice=Mock(),
            on_unconfirmed_phase_progress=Mock(), on_delivery_retry=Mock(),
        )

    def controller(self, ports, session, claims, stopping):
        transport = Transport(reply(start_response()), reply(observation()),
                              reply(response(ports.instructions.specification_bytes)), reply(progress_response()))
        delivery = RecordingDelivery()
        controller = AttemptController.live(
            api(transport, ports.pending.scope.api_origin), claims, session,
            scope=ports.pending.scope, stopping=stopping,
            start_policy=LiveStartPolicy(cadence_seconds=1, on_retry=Mock(), on_recovered=Mock()),
            allow_active_start_replay=True,
            delivery=delivery, on_delivery_retry=Mock(),
            delivery_retry_initial_seconds=0.01, delivery_retry_max_seconds=0.04,
            allowed_job_states=frozenset({"open"}), poll_seconds=1, task_settle_seconds=1,
        )
        return controller, transport, delivery

    async def startup_failure(self, *, hold_cleanup):
        ports, claims, stopping, retry = SessionPorts(), AttemptClaims.live(), asyncio.Event(), Mock()
        runner = RunningSupervisor(role="prediction", worker_argv=[sys.executable, "-c", "raise SystemExit(17)"])
        self.addCleanup(runner.close)
        cleanup_entered, release = asyncio.Event(), asyncio.Event()
        if not hold_cleanup:
            release.set()
        operations = []

        class HeldCleanup(NativeOperation):
            async def retire(self):
                cleanup_entered.set()
                await release.wait()
                await super().retire()

        def operation(**arguments):
            owned = HeldCleanup(**(arguments | {"endpoint": runner.path}))
            operations.append(owned)
            return owned

        factory = self.factory(ports, claims, retry)
        session = NMRNetSession(
            allocate_prediction=partial(factory.allocate_prediction, job_ref=ports.instructions.job_ref),
            execution_identity={"image_id": "fixture"}, selected_artifacts=ports.worker.artifacts,
            input_preparation=ports.preparation, on_prediction_retry=retry,
        )
        controller, transport, delivery = self.controller(ports, session, claims, stopping)
        with patch("nmrnet_provider.native_factory.NativeOperation", side_effect=operation):
            task = asyncio.create_task(controller.run(ports.pending))
            try:
                await asyncio.wait_for(delivery.accepted.wait(), 3)
                self.assertEqual(1, len(operations))
                self.assertTrue(factory.shutdown_ready)
                self.assertEqual([], factory.allocations)
                if hold_cleanup:
                    await asyncio.wait_for(cleanup_entered.wait(), 1)
                    self.assertFalse(controller.shutdown_ready)
                    self.assertTrue(claims.owns_job(ports.pending.scope, ports.instructions.job_ref))
                self.assertEqual("worker_crashed", json.loads(delivery.items[0].body)["failure_code"])
                self.assertIn("exit status 17", json.loads(delivery.items[0].body)["failure_message"])
            finally:
                release.set()
                accepted = await asyncio.wait_for(task, 3)
        self.assertIs(type(accepted), DeliveryAcceptedCheckpoint)
        self.assertTrue(controller.shutdown_ready)
        self.assertEqual((), claims.live_owners())
        self.assertEqual(1, len(delivery.items))
        self.assertEqual(4, len(transport.calls))
        retry.assert_not_called()
        crash = controller.operation.observed_crash
        self.assertIs(crash.owner, session)
        self.assertIs(crash.acknowledged.start, ports.pending)
        self.assertIs(crash.runner, operations[0].session)
        self.assertEqual("preparing", crash.phase)
        self.assertEqual(17, crash.native_exit.returncode)

    async def test_prediction_exit_before_startup_document_reports_once(self):
        await self.startup_failure(hold_cleanup=False)

    async def test_report_reaches_delivery_while_failed_startup_grant_still_needs_cleanup(self):
        await self.startup_failure(hold_cleanup=True)

    async def test_reader_crash_reaches_delivery_before_inline_input_cleanup(self):
        ports, claims, stopping, retry = SessionPorts(), AttemptClaims.live(), asyncio.Event(), Mock()
        raw = b"Predict shifts for this geometry (angstrom):\nH 0 0 0\n"
        ports.instructions = verify_job_input(response(raw), feed=facts(raw))
        ports.pending = replace(ports.pending, input=ports.instructions.identity)
        runner = RunningSupervisor(role="reader", worker_argv=[sys.executable, "-c", PARTIAL_REPLY_CHILD])
        self.addCleanup(runner.close)
        factory = self.factory(ports, claims, retry)
        cleanup_entered, release = asyncio.Event(), asyncio.Event()

        class HeldCleanup(NativeOperation):
            async def retire(self):
                cleanup_entered.set()
                await release.wait()
                await super().retire()

        def operation(**arguments):
            return HeldCleanup(**(arguments | {"endpoint": runner.path}))

        async def choose(_instructions, capability):
            await capability.prepare_pasted(source_text="H 0 0 0\n", source_format="table", source_unit="angstrom")

        preparation = ports.owner(choose, allocate_reader=factory.allocate_reader)
        no_prediction = AsyncMock(side_effect=AssertionError("Prediction cannot follow a failed structure read"))
        session = NMRNetSession(
            allocate_prediction=no_prediction, execution_identity={"image_id": "fixture"},
            selected_artifacts=ports.worker.artifacts, input_preparation=preparation, on_prediction_retry=retry,
        )
        controller, transport, delivery = self.controller(ports, session, claims, stopping)
        with patch("nmrnet_provider.native_factory.NativeOperation", side_effect=operation):
            task = asyncio.create_task(controller.run(ports.pending))
            try:
                await asyncio.wait_for(delivery.accepted.wait(), 3)
                await asyncio.wait_for(cleanup_entered.wait(), 1)
                self.assertFalse(controller.shutdown_ready)
                self.assertEqual("worker_crashed", json.loads(delivery.items[0].body)["failure_code"])
                self.assertEqual("reader", controller.operation.observed_crash.native_exit.role)
            finally:
                release.set()
                await asyncio.wait_for(task, 3)
        self.assertTrue(controller.shutdown_ready)
        self.assertEqual(1, len(delivery.items))
        self.assertEqual(4, len(transport.calls))
        no_prediction.assert_not_awaited()
        retry.assert_not_called()

    async def test_reader_exit_remains_primary_when_both_channel_close_paths_fail(self):
        runner = RunningSupervisor(role="reader", worker_argv=[sys.executable, "-c", PARTIAL_REPLY_CHILD])
        self.addCleanup(runner.close)
        operation = NativeOperation(role="reader", endpoint=runner.path, operation_seconds=2, observation_seconds=0.4, on_notice=Mock())
        reader, writer = await operation.start()
        client = GeometryClient(reader, writer, allocation=operation.allocation, operation_seconds=2, native=operation)
        try:
            with patch.object(writer, "close", side_effect=OSError("Authored close failure")):
                with self.assertRaises(NativeWorkerExited) as caught:
                    await client.read(b"H 0 0 0\n", source_format="table", source_unit="angstrom", selection=None)
            self.assertIs(caught.exception.native_operation, operation)
            self.assertEqual(17, operation.session.original_exit.returncode)
            self.assertIsInstance(caught.exception.__cause__, BaseExceptionGroup)
        finally:
            failures = await operation.retire_channel(client)
            self.assertEqual([], failures)
            await operation.settle()
