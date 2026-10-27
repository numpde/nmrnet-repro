"""Successful controller and source handoffs leave no factory-owned history."""

import asyncio
import unittest
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.fakes.attempt_store import record

from nmrnet_provider.native_factory import NativeAttemptFactory, NativePolicy
from tests.integration.nmrnet.retirement_ports import retire_worker_channel
from nmrnet_provider.smiles import GenerationSettings
from provider_core.controller import LiveStartPolicy
from provider_core.ownership import AttemptClaims
from provider_core.execution import ConditionRecovery, ObservationRecovery


class NativeFactoryTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_factory_releases_two_attempts_and_both_source_roles_on_transfer(self):
        settings = GenerationSettings(17, 80, 1)
        config = SimpleNamespace(preparation=SimpleNamespace(generation_settings=settings, generation_seconds=2))
        factory = NativeAttemptFactory(
            config=config,
            claims=AttemptClaims.live(),
            execution_identity={},
            selected_artifacts={},
            policy=NativePolicy(allocation_seconds=1, prediction_seconds=2, source_seconds=1),
            observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
            condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
            start_policy=LiveStartPolicy(1, Mock(), Mock()),
            on_prediction_retry=Mock(),
            on_operator_notice=Mock(),
            on_unconfirmed_phase_progress=Mock(),
            on_delivery_retry=Mock(),
        )
        ports = []
        stopping_events = []

        async def opened(role, *, job_ref):
            self.assertEqual(job_ref, "job:example" if role == "prediction" else None)
            worker = Mock(close=Mock(), wait_closed=AsyncMock())
            allocation = Mock(role=role, runtime={}, retire=AsyncMock(), settle=AsyncMock())
            allocation.retirement = allocation
            allocation.retire_channel = partial(retire_worker_channel, native_retirement=allocation)
            factory.allocations.append(allocation)
            factory.clients[allocation] = worker
            ports.append((worker, allocation))
            return worker, allocation

        async def assembled(**arguments):
            self.assertIs(arguments["start_policy"], factory.start_policy)
            self.assertIs(arguments["claims"], factory.claims)
            self.assertIs(arguments["stopping"], stopping)
            self.assertIs(arguments["on_delivery_retry"], factory.on_delivery_retry)
            self.assertIs(arguments["condition_recovery"], factory.condition_recovery)
            self.assertIs(arguments["on_prediction_retry"], factory.on_prediction_retry)
            self.assertEqual(arguments["allocate_prediction"].keywords, {"job_ref": "job:example"})
            self.assertIs(arguments["on_unconfirmed_phase_progress"], factory.on_unconfirmed_phase_progress)
            self.assertNotIn("api", arguments)
            self.assertNotIn("store", arguments)
            self.assertNotIn("setup_owner", arguments)
            self.assertNotIn("worker", arguments)
            self.assertNotIn("retirement", arguments)
            self.assertNotIn("runtime_expectations", arguments)
            return SimpleNamespace(allocate_prediction=arguments["allocate_prediction"])

        with (
            patch.object(factory, "_open_role", side_effect=opened),
            patch("nmrnet_provider.native_factory.assemble_attempt", side_effect=assembled),
        ):
            for _attempt in range(2):
                stopping = asyncio.Event()
                stopping_events.append(stopping)
                controller = await factory(record(), stopping=stopping)
                self.assertTrue(factory.shutdown_ready)
                self.assertEqual(factory.allocations, [])
                self.assertEqual(factory.clients, {})
                worker, retirement, runtime = await controller.allocate_prediction()
                reader, reader_retirement = await factory.allocate_reader()
                self.assertEqual(factory.allocations, [])
                self.assertEqual(factory.clients, {})
                generator, generator_retirement = await factory.allocate_generator(settings=settings, operation_seconds=2)
                self.assertEqual(factory.allocations, [])
                self.assertEqual(factory.clients, {})
                self.assertIs(worker, ports[-3][0])
                self.assertIs(reader, ports[-2][0])
                self.assertIs(generator, ports[-1][0])
                replacement, replacement_retirement, runtime = await factory.allocate_prediction(job_ref="job:example")
                self.assertIs(replacement, ports[-1][0])
                self.assertEqual(runtime, {})
                self.assertEqual(factory.allocations, [])
                self.assertEqual(factory.clients, {})
                for owner in (retirement, reader_retirement, generator_retirement, replacement_retirement):
                    owner.retire.assert_not_awaited()
                    await owner.retire()
        self.assertEqual(len(ports), 8)
        stopping_events[0].set()
        self.assertFalse(stopping_events[1].is_set())
