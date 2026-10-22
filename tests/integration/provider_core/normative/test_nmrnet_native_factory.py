"""Successful controller and source handoffs leave no factory-owned history."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.fakes.attempt_store import record

from nmrnet_provider.native_factory import NativeAttemptFactory, NativePolicy
from nmrnet_provider.smiles import GenerationSettings
from provider_core.execution import ObservationRecovery


class NativeFactoryTransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_factory_releases_two_attempts_and_both_source_roles_on_transfer(self):
        settings = GenerationSettings(17, 80, 1)
        config = SimpleNamespace(preparation=SimpleNamespace(generation_settings=settings, generation_seconds=2))
        store = SimpleNamespace(pending_operation=None, pending_owner=None)
        factory = NativeAttemptFactory(
            config=config,
            api=object(),
            store=store,
            claims=object(),
            execution_identity={},
            selected_artifacts={},
            policy=NativePolicy(allocation_seconds=1, prediction_seconds=2, source_seconds=1),
            observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
        )
        ports = []

        async def opened(role, *, job_ref):
            self.assertEqual(job_ref, "job:example" if role == "prediction" else None)
            worker = Mock(close=Mock(), wait_closed=AsyncMock())
            allocation = Mock(role=role, metadata=b'{"runtime":{}}', retire=AsyncMock(), settle=AsyncMock())
            allocation.retirement = allocation
            factory.allocations.append(allocation)
            factory.clients[allocation] = worker
            ports.append((worker, allocation))
            return worker, allocation

        async def assembled(**arguments):
            return SimpleNamespace(worker=arguments["worker"], retirement=arguments["retirement"])

        with (
            patch.object(factory, "_open_role", side_effect=opened),
            patch("nmrnet_provider.native_factory.assemble_attempt", side_effect=assembled),
        ):
            for _attempt in range(2):
                controller = await factory(record())
                self.assertEqual(factory.allocations, [])
                self.assertEqual(factory.clients, {})
                reader, reader_retirement = await factory.allocate_reader()
                self.assertEqual(factory.allocations, [])
                self.assertEqual(factory.clients, {})
                generator, generator_retirement = await factory.allocate_generator(settings=settings, operation_seconds=2)
                self.assertEqual(factory.allocations, [])
                self.assertEqual(factory.clients, {})
                self.assertEqual(factory.channel_closures, {})
                self.assertIs(controller.worker, ports[-3][0])
                self.assertIs(reader, ports[-2][0])
                self.assertIs(generator, ports[-1][0])
                for owner in (controller.retirement, reader_retirement, generator_retirement):
                    owner.retire.assert_not_awaited()
                    await owner.retire()
        self.assertEqual(len(ports), 6)
