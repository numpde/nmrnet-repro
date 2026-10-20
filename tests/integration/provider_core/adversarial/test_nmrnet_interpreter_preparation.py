"""An unresolved model conversation cannot produce a scientific handle or hide causes."""

import asyncio
import gc
import json
import unittest
import weakref
from types import SimpleNamespace
from unittest.mock import AsyncMock

from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import PreparationRetirementFailed
from nmrnet_provider.result import MAX_RESULT_BYTES, ResultRejected
from nmrnet_provider.session import PreparationEvidenceFailed
from provider_core.attempt_record import MAX_EVIDENCE_BYTES


class InterpreterPreparationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def choose(self, observation, _instructions, capability):
        reference = await capability.prepare_smiles(start_byte=7, end_byte=10, requested_count=2)
        capability.submit(reference)
        return observation

    async def test_retirement_failure_after_submit_blocks_the_handle(self):
        ports = SessionPorts()
        cleanup = OSError("PRIVATE model cleanup")

        async def choose(instructions, capability):
            return await self.choose(b"{}", instructions, capability)

        chooser = SimpleNamespace(run=choose, retire=AsyncMock(side_effect=cleanup))
        preparation = ports.owner(choose, chooser=chooser)
        session = ports.make_session(preparation)
        with self.assertRaises(PreparationRetirementFailed) as caught:
            await session.prepare(ports.acknowledged, ports.instructions)
        self.assertIsNone(caught.exception.operation_failure)
        self.assertIs(caught.exception.failures[0][1], cleanup)
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertEqual(ports.worker.events, [])
        chooser.retire.side_effect = None
        await session.retire()
        self.assertEqual(chooser.retire.await_count, 2)
        self.assertIs(preparation.last_retirement_failure, caught.exception)

    async def test_primary_failure_and_cancellation_survive_later_model_cleanup_failure(self):
        for primary in (OSError("PRIVATE operation"), asyncio.CancelledError()):
            with self.subTest(primary=type(primary).__name__):
                ports = SessionPorts()
                cleanup = OSError("PRIVATE cleanup")
                chooser = SimpleNamespace(run=AsyncMock(side_effect=primary), retire=AsyncMock(side_effect=cleanup))
                owner = ports.owner(chooser.run, chooser=chooser)
                if isinstance(primary, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        await owner.prepare(ports.instructions)
                    self.assertIs(caught.exception, primary)
                    observed = caught.exception
                    failure = caught.exception.__cause__
                else:
                    with self.assertRaises(PreparationRetirementFailed) as caught:
                        await owner.prepare(ports.instructions)
                    observed = primary
                    failure = caught.exception
                self.assertIs(failure.operation_failure, observed)
                self.assertIs(failure.failures[0][1], cleanup)
                chooser.retire.side_effect = None
                await owner.retire()
                self.assertEqual(chooser.retire.await_count, 2)

    async def test_full_retirement_still_attempts_source_transports_after_model_cleanup_failure(self):
        ports = SessionPorts()
        chooser = SimpleNamespace(run=AsyncMock(), retire=AsyncMock(side_effect=OSError("PRIVATE cleanup")))
        owner = ports.owner(chooser.run, chooser=chooser)
        with self.assertRaises(PreparationRetirementFailed):
            await owner.retire()
        for transport in ports.transports:
            transport.retire.assert_awaited_once()
        chooser.retire.side_effect = None
        await owner.retire()
        self.assertEqual(chooser.retire.await_count, 2)

    async def test_preparation_cause_survives_cancellation_and_a_later_chooser_cleanup_failure(self):
        ports = SessionPorts()
        rejection = InputRejected("Cannot prepare the selected geometry.", diagnostic="PRIVATE preparation evidence")
        cancelled = asyncio.CancelledError()
        cleanup = OSError("PRIVATE chooser cleanup")
        ports.generator.generate.side_effect = rejection
        ports.generator_host.retire.side_effect = cancelled

        async def choose(instructions, capability):
            return await self.choose(b"{}", instructions, capability)

        chooser = SimpleNamespace(run=choose, retire=AsyncMock(side_effect=cleanup))
        owner = ports.owner(choose, chooser=chooser)
        with self.assertRaises(asyncio.CancelledError) as caught:
            await owner.prepare(ports.instructions)
        self.assertIs(caught.exception, cancelled)
        latest = cancelled.__cause__
        self.assertIs(latest.operation_failure, cancelled)
        self.assertIs(latest.failures[0][1], cleanup)
        self.assertIs(latest.interruption_cause.operation_failure, rejection)
        self.assertIs(latest.interruption_cause.failures[0][1], cancelled)
        chooser.retire.side_effect = None
        ports.generator_host.retire.side_effect = None
        await owner.retire()

    async def test_retirement_after_submit_blocks_a_late_candidate_during_model_cleanup(self):
        ports = SessionPorts()
        closing, release, retirement_entered = (asyncio.Event() for _ in range(3))

        async def choose(instructions, capability):
            return await self.choose(b"{}", instructions, capability)

        async def close():
            closing.set()
            await release.wait()

        chooser = SimpleNamespace(run=choose, retire=AsyncMock(side_effect=close))
        owner = ports.owner(choose, chooser=chooser)

        async def retire():
            retirement_entered.set()
            await owner.retire()

        preparing = asyncio.create_task(owner.prepare(ports.instructions))
        await asyncio.wait_for(closing.wait(), 1)
        retiring = asyncio.create_task(retire())
        try:
            await asyncio.wait_for(retirement_entered.wait(), 1)
            self.assertFalse(retiring.done())
        finally:
            release.set()
            await asyncio.wait_for(retiring, 2)
            with self.assertRaisesRegex(ValueError, "retired"):
                await preparing
        chooser.retire.assert_awaited_once()

    async def test_mutable_observation_is_a_wiring_failure_and_still_retires_the_chooser(self):
        ports = SessionPorts()

        async def choose(instructions, capability):
            return await self.choose(bytearray(b"{}"), instructions, capability)

        chooser = SimpleNamespace(run=choose, retire=AsyncMock())
        owner = ports.owner(choose, chooser=chooser)
        self.addAsyncCleanup(owner.retire)
        with self.assertRaisesRegex(TypeError, "immutable"):
            await owner.prepare(ports.instructions)
        chooser.retire.assert_awaited_once()

    async def test_observation_exceeding_result_capacity_is_rejected_before_json_decode(self):
        ports = SessionPorts()

        async def choose(instructions, capability):
            return await self.choose(b"x" * (MAX_RESULT_BYTES + 1), instructions, capability)

        chooser = SimpleNamespace(run=choose, retire=AsyncMock())
        session = ports.make_session(ports.owner(choose, chooser=chooser))
        self.addAsyncCleanup(session.retire)
        with self.assertRaisesRegex(ResultRejected, "byte limit"):
            await session.prepare(ports.acknowledged, ports.instructions)
        self.assertEqual(ports.worker.events, [])

    async def test_concurrent_retirement_joins_the_run_and_closes_the_chooser_once(self):
        ports = SessionPorts()
        entered, cancelled, release, settled = (asyncio.Event() for _ in range(4))

        async def choose(_instructions, _capability):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
                raise
            finally:
                settled.set()

        chooser = SimpleNamespace(run=choose, retire=AsyncMock())
        owner = ports.owner(choose, chooser=chooser)
        preparing = asyncio.create_task(owner.prepare(ports.instructions))
        await asyncio.wait_for(entered.wait(), 1)
        retiring = asyncio.create_task(owner.retire())
        try:
            await asyncio.wait_for(cancelled.wait(), 1)
            self.assertFalse(retiring.done())
        finally:
            release.set()
            await asyncio.wait_for(retiring, 2)
            if not preparing.done():
                preparing.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await preparing
        self.assertTrue(settled.is_set())
        chooser.retire.assert_awaited_once()

    async def test_interpretation_evidence_capacity_blocks_scientific_dispatch(self):
        ports = SessionPorts()

        async def choose(instructions, capability):
            observation = json.dumps({"authored_large_observation": "x" * MAX_EVIDENCE_BYTES}).encode()
            return await self.choose(observation, instructions, capability)

        chooser = SimpleNamespace(run=choose, retire=AsyncMock())
        owner = ports.owner(choose, chooser=chooser)
        session = ports.make_session(owner)
        self.addAsyncCleanup(session.retire)
        with self.assertRaises(PreparationEvidenceFailed):
            await session.prepare(ports.acknowledged, ports.instructions)
        self.assertEqual(ports.worker.events, [])
        chooser.retire.assert_awaited_once()

    async def test_late_chooser_failure_does_not_reach_the_default_event_loop_logger(self):
        loop = asyncio.get_running_loop()
        original_handler = loop.get_exception_handler()
        reports = []
        release, finished = asyncio.Event(), asyncio.Event()

        def record_report(_loop, context):
            # Keep only the safe report category; retaining context would keep
            # its Task alive and invalidate the collection witness below.
            reports.append(context["message"])

        async def exercise():
            ports = SessionPorts()
            reference = None

            async def choose(_instructions, _capability):
                nonlocal reference
                reference = weakref.ref(asyncio.current_task())
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await release.wait()
                    raise RuntimeError("PRIVATE_LATE_CHOOSER") from None
                finally:
                    finished.set()

            chooser = SimpleNamespace(run=choose, retire=AsyncMock())
            owner = ports.owner(choose, chooser=chooser, seconds=0.02)
            with self.assertRaises(PreparationRetirementFailed):
                await owner.prepare(ports.instructions)
            release.set()
            await asyncio.wait_for(finished.wait(), 1)
            await asyncio.sleep(0)
            self.assertIsInstance(owner.chooser_failure, RuntimeError)
            return reference

        loop.set_exception_handler(record_report)
        try:
            reference = await exercise()
            gc.collect()
            await asyncio.sleep(0)
            gc.collect()
            self.assertIsNone(reference())
            self.assertEqual(reports, [])
        finally:
            release.set()
            loop.set_exception_handler(original_handler)
