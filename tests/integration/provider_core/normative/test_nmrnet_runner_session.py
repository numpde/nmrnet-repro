"""Original native setup and retirement tasks outlive cancellation of their waiter."""

import asyncio
import threading
import unittest
from unittest.mock import patch

from nmrnet_provider.native_session import NativeOperation
from provider_core.runner_session import RunnerSession
from tests.fakes.runner import RunningSupervisor


class NativeOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner = RunningSupervisor(role="reader")
        self.addCleanup(self.runner.close)

    def operation(self):
        return NativeOperation(role="reader", endpoint=self.runner.path, operation_seconds=2)

    async def test_opening_cancellation_stops_before_waiting_for_its_original_thread(self):
        admitted, release = threading.Event(), threading.Event()
        original = RunnerSession.connect

        def gated(session, **options):
            original(session, **options)
            admitted.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Test did not release the admitted opening thread")

        operation = self.operation()
        with patch.object(RunnerSession, "connect", gated):
            caller = asyncio.create_task(operation.start())
            self.assertTrue(await asyncio.to_thread(admitted.wait, 2))
            original_opening = operation._opening
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            retirement = asyncio.create_task(operation.retire())
            try:
                deadline = asyncio.get_running_loop().time() + 1
                while list(self.runner.scratch.iterdir()):
                    if asyncio.get_running_loop().time() >= deadline:
                        self.fail("Retirement waited for blocked opening before stopping its child")
                    await asyncio.sleep(0.01)
                self.assertFalse(original_opening.cancelled())
            finally:
                release.set()
            await retirement
            await operation.settle()
        self.assertIs(operation._opening, original_opening)
        self.assertTrue(original_opening.done())

    async def test_cancelled_retirement_waiter_keeps_original_observation(self):
        operation = self.operation()
        await operation.start()
        entered, release = threading.Event(), threading.Event()
        original = RunnerSession.retire

        def gated(session, **options):
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Test did not release retirement observation")
            return original(session, **options)

        with patch.object(RunnerSession, "retire", gated):
            caller = asyncio.create_task(operation.retire())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            retained = operation._retirement
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertFalse(retained.cancelled())
            release.set()
            await operation.retire()
            await operation.settle()
            self.assertIs(operation._retirement, retained)
        self.assertEqual(list(self.runner.scratch.iterdir()), [])

    async def test_lost_control_uses_one_retired_proof_session_without_scientific_commands(self):
        operation = self.operation()
        await operation.start()
        original = operation.session
        original.control.close()
        entered, release = threading.Event(), threading.Event()
        retire = RunnerSession.retire

        def gated(session, **options):
            if session is not original:
                entered.set()
                if not release.wait(timeout=3):
                    raise TimeoutError("Test did not release proof retirement")
            return retire(session, **options)

        with patch.object(RunnerSession, "retire", gated):
            caller = asyncio.create_task(operation.retire())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            proof_task = operation._proof_task
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertFalse(proof_task.cancelled())
            release.set()
            await operation.retire()
            self.assertIs(operation._proof_task, proof_task)
        await operation.settle()
        self.assertIsNot(operation.proof, original)
        self.assertIsNotNone(operation.proof.retirement)
        self.assertTrue(operation.proof.retirement.scratch_released)
        self.assertIsNotNone(operation.last_failure)
        proof = operation.proof
        await operation.retire()
        self.assertIs(operation.proof, proof)
        self.assertEqual(list(self.runner.scratch.iterdir()), [])
        self.runner.close()
        self.assertNotIn(b"event=fixture_scientific_input", self.runner.diagnostic)

    async def test_failed_proof_setup_revokes_its_owned_connection_and_keeps_failure_held(self):
        operation = self.operation()
        await operation.start()
        original = operation.session
        original.control.close()
        connect = RunnerSession.connect

        def failed_after_admission(session, **options):
            connect(session, **options)
            raise RuntimeError("Injected proof setup failure after descriptor admission")

        with patch.object(RunnerSession, "connect", failed_after_admission):
            with self.assertRaisesRegex(RuntimeError, "Injected proof"):
                await operation.retire()
        self.assertEqual(operation.proof.control.fileno(), -1)
        self.assertEqual(operation.proof.science.fileno(), -1)
        with self.assertRaisesRegex(RuntimeError, "Injected proof"):
            await operation.settle()
        original.close()
        deadline = asyncio.get_running_loop().time() + 1
        while list(self.runner.scratch.iterdir()):
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("Failed proof setup left its native child admitted")
            await asyncio.sleep(0.01)

    async def test_expired_cleanup_does_not_acquire_a_fresh_proof_child(self):
        operation = NativeOperation(role="reader", endpoint=self.runner.path, operation_seconds=0.3)
        await operation.start()
        entered, release = threading.Event(), threading.Event()

        def delayed_failure(session, **options):
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("Test did not release delayed control failure")
            raise OSError("Injected control loss after its cleanup deadline")

        with patch.object(RunnerSession, "retire", delayed_failure):
            caller = asyncio.create_task(operation.retire())
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            try:
                with self.assertRaises(TimeoutError):
                    await caller
            finally:
                release.set()
            await asyncio.gather(operation._control_retirement, return_exceptions=True)
            with self.assertRaises(TimeoutError):
                await operation.retire()
        self.assertIsNone(operation.proof)
        operation.session.close()
        await operation.writer.wait_closed()
