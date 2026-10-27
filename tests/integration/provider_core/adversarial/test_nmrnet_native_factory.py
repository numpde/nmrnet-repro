"""Retain original native setup and cleanup until their live owners settle."""

import asyncio
import unittest
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests.fakes.attempt_store import record

from nmrnet_provider.native_factory import NativeAttemptFactory, NativePolicy, NativeSetupFailed
from tests.integration.nmrnet.retirement_ports import retire_worker_channel
from provider_core.controller import LiveStartPolicy
from provider_core.ownership import AttemptClaims
from provider_core.execution import ConditionRecovery, ObservationRecovery


class NativeFactoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_controller_assembly_needs_no_native_cleanup(self):
        factory = self.factory(seconds=1)
        primary = RuntimeError("PRIVATE_ASSEMBLY_CAUSE")
        with (
            patch("nmrnet_provider.native_factory.NativeOperation") as allocation,
            patch("nmrnet_provider.native_factory.assemble_attempt", new=AsyncMock(side_effect=primary)),
        ):
            with self.assertRaises(NativeSetupFailed) as failure:
                await factory(record(), stopping=asyncio.Event())
            self.assertIs(failure.exception.cause, primary)
            self.assertEqual(failure.exception.cleanup_failures, ())
            self.assertEqual(failure.exception.job_ref, "job:example")
            self.assertNotIn("PRIVATE_", str(failure.exception))
            self.assertIsNone(factory.last_failure)
            self.assertTrue(factory.shutdown_ready)
            await factory.settle()
            allocation.assert_not_called()

    async def test_active_assembly_excludes_another_allocation_and_settlement(self):
        factory = self.factory(seconds=1)
        entered, release = asyncio.Event(), asyncio.Event()
        controller = object()

        async def assembled(**_arguments):
            entered.set()
            await release.wait()
            return controller

        with (
            patch("nmrnet_provider.native_factory.NativeOperation") as allocation,
            patch("nmrnet_provider.native_factory.assemble_attempt", new=AsyncMock(side_effect=assembled)),
        ):
            task = asyncio.create_task(factory(record(), stopping=asyncio.Event()))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.assertFalse(factory.shutdown_ready)
                with self.assertRaisesRegex(RuntimeError, "await active setup"):
                    await factory.allocate_prediction(job_ref="job:example")
                with self.assertRaisesRegex(RuntimeError, "original invocation is active"):
                    await factory.settle()
                allocation.assert_not_called()
            finally:
                release.set()
                self.assertIs(await task, controller)
        self.assertTrue(factory.shutdown_ready)

    async def test_unsettled_allocation_io_keeps_the_original_grant_until_settlement(self):
        factory = self.factory(seconds=1)
        allocation = self.allocation()
        primary = RuntimeError("PRIVATE_CLIENT_SETUP")
        pending_io = TimeoutError("PRIVATE_ORIGINAL_ALLOCATION_IO")
        allocation.settle.side_effect = pending_io
        with (
            patch("nmrnet_provider.native_factory.NativeOperation", return_value=allocation) as created,
            patch("nmrnet_provider.native_factory.PredictionClient", side_effect=primary),
            patch("nmrnet_provider.native_factory.assemble_attempt", new=AsyncMock()) as assembled,
        ):
            with self.assertRaises(NativeSetupFailed) as failure:
                await factory.allocate_prediction(job_ref="job:example")
            self.assertIs(failure.exception.cause, primary)
            self.assertEqual(failure.exception.cleanup_failures, (("allocation I/O", pending_io),))
            self.assertEqual(factory.allocations, [allocation])
            self.assertEqual(factory.clients, {})
            self.assertFalse(factory.shutdown_ready)
            with self.assertRaisesRegex(RuntimeError, "original obligations"):
                await factory(record(), stopping=asyncio.Event())
            assembled.assert_not_awaited()
            allocation.settle.side_effect = None
            await factory.settle()
            created.assert_called_once()
        allocation.start.assert_awaited_once()
        self.assertEqual(allocation.retire.await_count, 2)
        self.assertEqual(allocation.settle.await_count, 2)
        self.assertIsNone(factory.last_failure)
        self.assertTrue(factory.shutdown_ready)

    async def test_failed_setup_retains_both_native_retirement_and_allocation_io_failures(self):
        factory = self.factory(seconds=1)
        allocation = self.allocation()
        primary = OSError("PRIVATE_SETUP_IO")
        retirement = TimeoutError("PRIVATE_NATIVE_RETIREMENT")
        pending_io = OSError("PRIVATE_ALLOCATION_SETTLEMENT")
        allocation.start.side_effect = primary
        allocation.retire.side_effect = retirement
        allocation.settle.side_effect = pending_io
        with (
            patch("nmrnet_provider.native_factory.NativeOperation", return_value=allocation),
            patch("nmrnet_provider.native_factory.PredictionClient") as client,
            self.assertRaises(NativeSetupFailed) as failure,
        ):
            await factory.allocate_prediction(job_ref="job:example")
        self.assertIs(failure.exception.cause, primary)
        self.assertEqual(failure.exception.cleanup_failures, (
            ("native retirement", retirement), ("allocation I/O", pending_io),
        ))
        self.assertEqual(failure.exception.job_ref, "job:example")
        self.assertEqual(factory.allocations, [allocation])
        self.assertFalse(factory.shutdown_ready)
        self.assertNotIn("PRIVATE_", str(failure.exception))
        client.assert_not_called()
        allocation.retire.side_effect = allocation.settle.side_effect = None
        await factory.settle()
        self.assertTrue(factory.shutdown_ready)

    async def test_controller_assembly_does_not_allocate_prediction_before_start(self):
        factory = self.factory(seconds=1)
        controller = object()
        with (
            patch.object(factory, "_open_role", new=AsyncMock(side_effect=AssertionError("Prediction allocated before START"))) as opened,
            patch("nmrnet_provider.native_factory.assemble_attempt", new=AsyncMock(return_value=controller)) as assembly,
        ):
            self.assertIs(await factory(record(), stopping=asyncio.Event()), controller)
        opened.assert_not_called()
        self.assertEqual(factory.allocations, [])
        self.assertEqual(factory.clients, {})
        failure = RuntimeError("Fixture model refusal")
        assembly.call_args.kwargs["on_interpreter_failure"](1, failure)
        factory.on_operator_notice.assert_called_once_with(
            failure, {"event": "interpreter_endpoint_failed", "job_ref": record().input.job_ref, "endpoint_index": 1}
        )

    async def test_missing_condition_recovery_is_refused_before_native_allocation(self):
        with (
            patch("nmrnet_provider.native_factory.NativeOperation") as allocation,
            self.assertRaisesRegex(ValueError, "condition recovery"),
        ):
            NativeAttemptFactory(
                config=object(),
                claims=AttemptClaims.live(),
                execution_identity={},
                selected_artifacts={},
                policy=NativePolicy(allocation_seconds=1, prediction_seconds=1, source_seconds=1),
                observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
                condition_recovery=None,
                start_policy=LiveStartPolicy(1, Mock(), Mock()),
                on_delivery_retry=Mock(),
                on_prediction_retry=Mock(),
                on_operator_notice=Mock(),
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
                claims=AttemptClaims.live(),
                execution_identity={},
                selected_artifacts={},
                policy=NativePolicy(allocation_seconds=1, prediction_seconds=1, source_seconds=1),
                observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
                condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
                start_policy=LiveStartPolicy(1, Mock(), Mock()),
                on_delivery_retry=Mock(),
                on_prediction_retry=Mock(),
                on_operator_notice=Mock(),
                on_unconfirmed_phase_progress=None,
            )
        allocation.assert_not_called()

    async def test_missing_start_policy_is_refused_before_native_allocation(self):
        with (
            patch("nmrnet_provider.native_factory.NativeOperation") as allocation,
            self.assertRaisesRegex(ValueError, "live START policy"),
        ):
            NativeAttemptFactory(
                config=object(),
                claims=AttemptClaims.live(),
                execution_identity={},
                selected_artifacts={},
                policy=NativePolicy(allocation_seconds=1, prediction_seconds=1, source_seconds=1),
                observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
                condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
                start_policy=None,
                on_prediction_retry=Mock(),
                on_operator_notice=Mock(),
                on_unconfirmed_phase_progress=Mock(),
                on_delivery_retry=Mock(),
            )
        allocation.assert_not_called()

    async def test_missing_stop_event_is_refused_before_native_allocation(self):
        factory = self.factory(seconds=1)
        with (
            patch("nmrnet_provider.native_factory.NativeOperation") as allocation,
            patch("nmrnet_provider.native_factory.assemble_attempt", new=AsyncMock()) as assembled,
            self.assertRaises(TypeError),
        ):
            await factory(record())
        allocation.assert_not_called()
        assembled.assert_not_awaited()
        self.assertTrue(factory.shutdown_ready)

    async def test_missing_delivery_retry_observer_is_refused_before_native_allocation(self):
        with (
            patch("nmrnet_provider.native_factory.NativeOperation") as allocation,
            self.assertRaisesRegex(ValueError, "delivery retry observer"),
        ):
            NativeAttemptFactory(
                config=object(),
                claims=AttemptClaims.live(),
                execution_identity={},
                selected_artifacts={},
                policy=NativePolicy(allocation_seconds=1, prediction_seconds=1, source_seconds=1),
                observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
                condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
                start_policy=LiveStartPolicy(1, Mock(), Mock()),
                on_prediction_retry=Mock(),
                on_operator_notice=Mock(),
                on_unconfirmed_phase_progress=Mock(),
                on_delivery_retry=None,
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
            claims=AttemptClaims.live(),
            execution_identity={},
            selected_artifacts={},
            policy=NativePolicy(allocation_seconds=seconds, prediction_seconds=2, source_seconds=1),
            observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
            condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
            start_policy=LiveStartPolicy(1, Mock(), Mock()),
            on_prediction_retry=Mock(),
            on_operator_notice=Mock(),
            on_unconfirmed_phase_progress=Mock(),
            on_delivery_retry=Mock(),
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
