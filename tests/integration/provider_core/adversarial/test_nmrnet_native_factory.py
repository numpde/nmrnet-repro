"""Native setup refusal keeps allocations and original store I/O with the factory."""

import asyncio
import unittest
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.fakes.attempt_store import record

from nmrnet_provider.native_factory import NativeAttemptFactory, NativePolicy, NativeSetupFailed
from tests.integration.nmrnet.retirement_ports import retire_worker_channel
from provider_core.controller import StartRecovery, TerminalRecovery
from provider_core.execution import ConditionRecovery, ObservationRecovery


class NativeFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_assembly_does_not_allocate_prediction_before_start(self):
        factory = self.factory(seconds=1)
        controller = object()
        with (
            patch.object(factory, "_open_role", new=AsyncMock(side_effect=AssertionError("Prediction allocated before START"))) as opened,
            patch("nmrnet_provider.native_factory.assemble_attempt", new=AsyncMock(return_value=controller)),
        ):
            self.assertIs(await factory(record()), controller)
        opened.assert_not_called()
        self.assertEqual(factory.allocations, [])
        self.assertEqual(factory.clients, {})

    async def test_missing_condition_recovery_is_refused_before_native_allocation(self):
        with (
            patch("nmrnet_provider.native_factory.NativeOperation") as allocation,
            self.assertRaisesRegex(ValueError, "condition recovery"),
        ):
            NativeAttemptFactory(
                config=object(),
                api=object(),
                store=object(),
                claims=object(),
                execution_identity={},
                selected_artifacts={},
                policy=NativePolicy(allocation_seconds=1, prediction_seconds=1, source_seconds=1),
                observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
                condition_recovery=None,
                start_recovery=StartRecovery(1, Mock(), Mock()),
                terminal_recovery=TerminalRecovery(1, Mock(), Mock()),
                on_prediction_retry=Mock(),
                on_native_notice=Mock(),
                on_unconfirmed_phase_progress=Mock(),
            )
        allocation.assert_not_called()

    async def test_missing_phase_diagnostic_observer_is_refused_before_native_allocation(self):
        with (
            patch("nmrnet_provider.native_factory.NativeOperation") as allocation,
            self.assertRaisesRegex(ValueError, "phase-progress"),
        ):
            NativeAttemptFactory(
                config=object(),
                api=object(),
                store=object(),
                claims=object(),
                execution_identity={},
                selected_artifacts={},
                policy=NativePolicy(allocation_seconds=1, prediction_seconds=1, source_seconds=1),
                observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
                condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
                start_recovery=StartRecovery(1, Mock(), Mock()),
                terminal_recovery=TerminalRecovery(1, Mock(), Mock()),
                on_prediction_retry=Mock(),
                on_native_notice=Mock(),
                on_unconfirmed_phase_progress=None,
            )
        allocation.assert_not_called()

    async def test_missing_start_recovery_is_refused_before_native_allocation(self):
        with (
            patch("nmrnet_provider.native_factory.NativeOperation") as allocation,
            self.assertRaisesRegex(ValueError, "START recovery"),
        ):
            NativeAttemptFactory(
                config=object(),
                api=object(),
                store=object(),
                claims=object(),
                execution_identity={},
                selected_artifacts={},
                policy=NativePolicy(allocation_seconds=1, prediction_seconds=1, source_seconds=1),
                observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
                condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
                start_recovery=None,
                on_prediction_retry=Mock(),
                on_native_notice=Mock(),
                on_unconfirmed_phase_progress=Mock(),
                terminal_recovery=TerminalRecovery(1, Mock(), Mock()),
            )
        allocation.assert_not_called()

    async def test_missing_terminal_recovery_is_refused_before_native_allocation(self):
        with (
            patch("nmrnet_provider.native_factory.NativeOperation") as allocation,
            self.assertRaisesRegex(ValueError, "terminal recovery"),
        ):
            NativeAttemptFactory(
                config=object(),
                api=object(),
                store=object(),
                claims=object(),
                execution_identity={},
                selected_artifacts={},
                policy=NativePolicy(allocation_seconds=1, prediction_seconds=1, source_seconds=1),
                observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
                condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
                start_recovery=StartRecovery(1, Mock(), Mock()),
                on_prediction_retry=Mock(),
                on_native_notice=Mock(),
                on_unconfirmed_phase_progress=Mock(),
                terminal_recovery=None,
            )
        allocation.assert_not_called()

    async def test_cancellation_during_cleanup_remains_interruption_after_ordinary_setup_failure(self):
        factory = self.factory(seconds=1)
        allocation = self.allocation()
        entered = asyncio.Event()
        observed = []
        prior = OSError("PRIVATE_CLEANUP_WAIT_CAUSE")
        primary = RuntimeError("PRIVATE_CLIENT_SETUP")
        writer = Mock(close=Mock(), wait_closed=AsyncMock())

        async def held_retirement():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as interruption:
                interruption.__cause__ = prior
                observed.append(interruption)
                raise

        allocation.retire.side_effect = held_retirement
        with (
            patch("nmrnet_provider.native_factory.NativeOperation", return_value=allocation),
            patch.object(allocation, "start", new=AsyncMock(return_value=(object(), writer))),
            patch("nmrnet_provider.native_factory.PredictionClient", side_effect=primary),
        ):
            task = asyncio.create_task(factory.allocate_prediction(job_ref="job:example"))
            async with asyncio.timeout(2):
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError) as failure:
                    await task
        self.assertIs(failure.exception, observed[0])
        self.assertIs(factory.last_failure.cause, primary)
        self.assertIs(factory.last_failure.interruption_cause, prior)
        self.assertEqual(factory.last_failure.cleanup_failures, (("native retirement", observed[0]),))
        self.assertEqual(factory.last_failure.job_ref, "job:example")

    def factory(self, *, seconds):
        return NativeAttemptFactory(
            config=object(),
            api=object(),
            store=SimpleNamespace(pending_operation=None, pending_owner=None, settle=AsyncMock()),
            claims=object(),
            execution_identity={},
            selected_artifacts={},
            policy=NativePolicy(allocation_seconds=seconds, prediction_seconds=2, source_seconds=1),
            observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
            condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
            start_recovery=StartRecovery(1, Mock(), Mock()),
            on_prediction_retry=Mock(),
            on_native_notice=Mock(),
            on_unconfirmed_phase_progress=Mock(),
            terminal_recovery=TerminalRecovery(1, Mock(), Mock()),
        )

    def allocation(self):
        allocation = Mock(allocation="a" * 32, ipc=Path("/unused"), start=AsyncMock(return_value=(object(), Mock(close=Mock(), wait_closed=AsyncMock()))), retire=AsyncMock(), settle=AsyncMock())
        allocation.retirement = allocation
        allocation.retire_channel = partial(retire_worker_channel, native_retirement=allocation)
        return allocation

    async def test_cancellation_keeps_its_original_cause_and_failed_native_retirement_through_both_layers(self):
        factory = self.factory(seconds=1)
        allocation = self.allocation()
        interrupted = asyncio.CancelledError()
        original = OSError("PRIVATE_INTERRUPTED_SETUP")
        interrupted.__cause__ = original
        cleanup = OSError("PRIVATE_NATIVE_RETIREMENT")
        allocation.start.side_effect = interrupted
        allocation.retire.side_effect = cleanup
        with (
            patch("nmrnet_provider.native_factory.NativeOperation", return_value=allocation),
            self.assertRaises(asyncio.CancelledError) as failure,
        ):
            await factory.allocate_prediction(job_ref="job:example")
        self.assertIs(failure.exception, interrupted)
        self.assertIs(interrupted.__cause__, factory.last_failure)
        self.assertIs(factory.last_failure.interruption_cause, original)
        self.assertEqual(factory.last_failure.cleanup_failures, (("native retirement", cleanup),))
        self.assertNotIn("PRIVATE_", str(factory.last_failure))
        self.assertEqual(factory.last_failure.job_ref, "job:example")
        with self.assertRaises(asyncio.CancelledError):
            await factory.settle()
        self.assertEqual(factory.last_failure.job_ref, "job:example")

    async def test_owned_store_read_without_native_entries_blocks_setup_until_explicit_settlement(self):
        factory = self.factory(seconds=1)
        store = SimpleNamespace(pending_operation="original read", pending_owner=factory, settle=AsyncMock())
        factory.store = store

        async def settled(*, owner):
            self.assertIs(owner, factory)
            store.pending_operation = store.pending_owner = None

        store.settle.side_effect = settled
        with patch.object(factory, "_open_role") as allocate, self.assertRaisesRegex(RuntimeError, "settle"):
            await factory(record())
        allocate.assert_not_called()
        await factory.settle()
        store.settle.assert_awaited_once_with(owner=factory)
        self.assertEqual(factory.allocations, [])

    async def test_failed_assembly_settles_owned_setup_read_without_allocating_prediction(self):
        events = []
        store = SimpleNamespace(pending_operation=None, pending_owner=None, settle=AsyncMock())
        allocation = Mock(metadata=b'{"runtime":{}}', retire=AsyncMock(), settle=AsyncMock())
        allocation.retirement = allocation
        allocation.retire_channel = partial(retire_worker_channel, native_retirement=allocation)
        worker = Mock(close=Mock(), wait_closed=AsyncMock())
        primary = RuntimeError("PRIVATE_ASSEMBLY_CAUSE")
        factory = NativeAttemptFactory(
            config=object(),
            api=object(),
            store=store,
            claims=object(),
            execution_identity={},
            selected_artifacts={},
            policy=NativePolicy(allocation_seconds=1, prediction_seconds=2, source_seconds=1),
            observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
            condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
            start_recovery=StartRecovery(1, Mock(), Mock()),
            on_prediction_retry=Mock(),
            on_native_notice=Mock(),
            on_unconfirmed_phase_progress=Mock(),
            terminal_recovery=TerminalRecovery(1, Mock(), Mock()),
        )

        async def opened(_role, *, job_ref):
            self.assertEqual(job_ref, "job:example")
            factory.allocations.append(allocation)
            factory.clients[allocation] = worker
            return worker, allocation

        async def settle_store(*, owner):
            self.assertIs(owner, factory)
            events.append("store")
            store.pending_operation = store.pending_owner = None

        async def failed_assembly(**arguments):
            self.assertIs(arguments["setup_owner"], factory)
            store.pending_operation, store.pending_owner = object(), factory
            raise primary

        store.settle.side_effect = settle_store
        with (
            patch.object(factory, "_open_role", side_effect=opened) as acquire,
            patch("nmrnet_provider.native_factory.assemble_attempt", side_effect=failed_assembly),
            self.assertRaises(RuntimeError) as failure,
        ):
            await factory(record())
        self.assertIs(failure.exception.cause, primary)
        self.assertNotIn("PRIVATE_ASSEMBLY_CAUSE", str(failure.exception))
        self.assertEqual(events, ["store"])
        acquire.assert_not_called()
        allocation.retire.assert_not_awaited()
        allocation.settle.assert_not_awaited()
        worker.close.assert_not_called()
        worker.wait_closed.assert_not_called()

    async def test_refused_assembly_and_later_settlement_never_steal_an_unrelated_unowned_read(self):
        factory = self.factory(seconds=1)
        original = object()
        factory.store.pending_operation = original
        allocation = self.allocation()
        allocation.metadata = b'{"runtime":{}}'
        worker = Mock(close=Mock(), wait_closed=AsyncMock())

        async def opened(_role, *, job_ref):
            self.assertEqual(job_ref, "job:example")
            factory.allocations.append(allocation)
            factory.clients[allocation] = worker
            return worker, allocation

        with (
            patch.object(factory, "_open_role", side_effect=opened),
            patch("nmrnet_provider.native_factory.assemble_attempt", side_effect=RuntimeError("store already busy")),
            self.assertRaises(RuntimeError),
        ):
            await factory(record())
        await factory.settle()
        factory.store.settle.assert_not_awaited()
        self.assertIs(factory.store.pending_operation, original)
        self.assertIsNone(factory.store.pending_owner)
        self.assertEqual(factory.allocations, [])

    async def test_held_store_read_does_not_acquire_prediction(self):
        factory = self.factory(seconds=1)
        allocation = self.allocation()
        allocation.metadata = b'{"runtime":{}}'
        worker = Mock(close=Mock(), wait_closed=AsyncMock())
        original_read = object()
        store_failure = OSError("PRIVATE_STORE_SETTLEMENT")

        async def opened(_role, *, job_ref):
            self.assertEqual(job_ref, "job:example")
            factory.allocations.append(allocation)
            factory.clients[allocation] = worker
            return worker, allocation

        async def interrupted_assembly(**arguments):
            self.assertIs(arguments["setup_owner"], factory)
            factory.store.pending_operation, factory.store.pending_owner = original_read, factory
            raise RuntimeError("PRIVATE_ASSEMBLY")

        factory.store.settle.side_effect = store_failure
        with (
            patch.object(factory, "_open_role", side_effect=opened),
            patch("nmrnet_provider.native_factory.assemble_attempt", side_effect=interrupted_assembly),
            self.assertRaises(RuntimeError) as failure,
        ):
            await factory(record())
        self.assertEqual(failure.exception.cleanup_failures, (("setup storage read", store_failure),))
        self.assertIs(factory.store.pending_operation, original_read)
        self.assertEqual(factory.allocations, [])
        self.assertEqual(factory.clients, {})
        allocation.retire.assert_not_awaited()
        with self.assertRaisesRegex(RuntimeError, "settle"):
            await factory(record())

        async def settled(*, owner):
            self.assertIs(owner, factory)
            self.assertIs(factory.store.pending_operation, original_read)
            factory.store.pending_operation = factory.store.pending_owner = None

        factory.store.settle.side_effect = settled
        await factory.settle()
        self.assertIsNone(factory.last_failure)
        allocation.retire.assert_not_awaited()
