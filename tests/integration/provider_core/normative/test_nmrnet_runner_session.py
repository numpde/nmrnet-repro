"""Original native setup and retirement tasks outlive cancellation of their waiter."""

import asyncio
import errno
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from nmrnet_provider.native_session import NativeOperation
from provider_core.runner_session import RunnerSession, RunnerStartupStopped, RunnerStartupUnavailable
from tests.fakes.runner import RunningSupervisor


class NativeLeaseTests(unittest.TestCase):
    def test_fifo_lease_rejects_without_waiting_for_a_writer_or_allocating_a_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.mkfifo(root / ".owner-session.lock", mode=0o600)
            code = """
import sys
sys.path.append('/consumer')
from pathlib import Path
from nmrnet_provider.native_session import NativeOperation
operation = NativeOperation(role='reader', endpoint=Path(sys.argv[1]) / 'runner.sock',
                            operation_seconds=1, on_notice=lambda notice: None)
try:
    operation._connect()
except PermissionError as failure:
    assert 'regular file' in str(failure), str(failure)
else:
    raise AssertionError('A FIFO must not admit a native session')
assert operation.session is None
"""
            result = subprocess.run([sys.executable, "-c", code, str(root)],
                                    capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)


class NativeOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_before_connect_does_not_allocate_a_retirement_child(self):
        entered, stopped, release = threading.Event(), threading.Event(), threading.Event()
        connect = RunnerSession.connect
        allocations = []
        operation = self.operation()
        stop = operation._request_stop

        def gated_session(*arguments, **options):
            if not entered.is_set():
                entered.set()
                if not release.wait(timeout=3):
                    raise TimeoutError("Fixture did not release the stopped connection")
            return RunnerSession(*arguments, **options)

        def observed_connect(session, **options):
            try:
                return connect(session, **options)
            finally:
                if session.operation is not None:
                    allocations.append(session.operation)

        def requested_stop():
            stop()
            stopped.set()

        self.addAsyncCleanup(operation.retire)
        with (patch("nmrnet_provider.native_session.RunnerSession", side_effect=gated_session),
              patch.object(RunnerSession, "connect", observed_connect),
              patch.object(operation, "_request_stop", requested_stop)):
            caller = asyncio.create_task(operation.start())
            retirement = None
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                caller.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
                retirement = asyncio.create_task(operation.retire())
                self.assertTrue(await asyncio.to_thread(stopped.wait, 2))
                self.assertIsNone(operation.session)
            finally:
                release.set()
                if retirement is not None:
                    await retirement
                else:
                    await operation.retire()
            await operation.settle()
        self.assertEqual(allocations, [])
        self.assertIsNone(operation.allocation)
        self.assertEqual(operation.session.control.fileno(), -1)
        self.assertIsInstance(self.notices.call_args.args[0], RunnerStartupStopped)

    async def test_never_connected_readiness_expiry_retires_without_a_proof_child(self):
        self.runner.process.kill()
        await asyncio.to_thread(self.runner.process.wait, timeout=2)
        operation = self.operation()
        operation._seconds = 0.2
        try:
            with self.assertRaises(TimeoutError):
                await operation.start()
            await asyncio.gather(operation._opening, return_exceptions=True)
            await operation.retire()
            await operation.settle()
            self.assertIsNone(operation.allocation)
            self.assertIsNone(operation.proof)
            self.assertEqual(operation.session.control.fileno(), -1)
            self.assertIsInstance(self.notices.call_args.args[0], RunnerStartupUnavailable)
        finally:
            if operation.session is not None:
                operation.session.close()
            if operation.proof is not None:
                operation.proof.close()

    async def test_retirement_barrier_does_not_adopt_or_send_scientific_input(self):
        operation = self.operation()
        with patch("nmrnet_provider.native_session.asyncio.open_connection", side_effect=AssertionError("Scientific channel adopted")):
            await operation.connect_for_retirement()
            self.assertIsNotNone(operation.allocation)
            self.assertIsNone(operation.reader)
            self.assertIsNone(operation.writer)
            self.assertIsNone(operation.runtime)
            await operation.retire()
            await operation.settle()
        self.assertTrue(operation.session.retirement.scratch_released)
        self.assertEqual(list(self.runner.scratch.iterdir()), [])
        self.runner.close()
        self.assertNotIn(b"event=fixture_scientific_input", self.runner.diagnostic)

    async def test_cancelled_retirement_barrier_retains_its_original_connection(self):
        operation = self.operation()
        entered, release = threading.Event(), threading.Event()
        connect = operation._connect

        def held_connect():
            entered.set()
            if not release.wait(timeout=3):
                raise TimeoutError("Fixture did not release barrier connection")
            connect()

        with patch.object(operation, "_connect", held_connect):
            caller = asyncio.create_task(operation.connect_for_retirement())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                original = operation._opening
                caller.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await caller
                self.assertFalse(original.cancelled())
            finally:
                release.set()
                await operation.retire()
                await operation.settle()
        self.assertIs(operation._opening, original)
        self.assertEqual(list(self.runner.scratch.iterdir()), [])

    async def test_pending_close_deadline_keeps_original_observation_held(self):
        operation = self.operation()
        await operation.start()
        operation._seconds = 0.1
        release = asyncio.Event()
        original = operation.writer.wait_closed
        close = operation.writer.close
        transport_socket = operation.writer.transport.get_extra_info("socket")

        async def gated():
            await release.wait()
            close()
            await original()

        with (patch.object(operation.writer, "wait_closed", side_effect=gated),
              patch.object(operation.writer, "close")):
            try:
                with self.assertRaises(TimeoutError):
                    await operation.retire()
                self.assertFalse(operation._science_closure.done())
                self.assertEqual(operation.session.control.fileno(), -1)
                self.assertGreaterEqual(transport_socket.fileno(), 0)
                self.assertIsNone(operation.session.science)
                self.notices.assert_not_called()
            finally:
                release.set()
            await operation._science_closure
            await operation.retire()
            await operation.settle()

    async def test_completed_transport_reset_is_reported_without_holding_retired_resources(self):
        operation = self.operation()
        await operation.start()
        reset = ConnectionResetError(104, "fixture transport reset")
        operation.writer.transport._force_close(reset)
        await asyncio.sleep(0)
        operation.session.control.close()  # Force the independent proof-session path.
        worker = Mock(close=operation.writer.close)
        original = operation.writer.wait_closed
        with patch.object(operation.writer, "wait_closed", wraps=original) as closed:
            self.assertEqual(await operation.retire_channel(worker), [])
            await operation.retire()
            await operation.settle()
            closed.assert_awaited_once()
        worker.wait_closed.assert_not_called()
        self.assertEqual(operation.session.control.fileno(), -1)
        self.assertIsNotNone(operation.proof)
        self.assertEqual(operation.proof.control.fileno(), -1)
        self.assertEqual(list(self.runner.scratch.iterdir()), [])
        self.notices.assert_called_once()
        evidence = self.notices.call_args.args[0]
        self.assertTrue(evidence is reset or reset in evidence.exceptions)

    async def test_channel_retirement_records_interruption_before_closing_science(self):
        operation = self.operation()
        await operation.start()
        interrupted_at_close = []

        def close():
            interrupted_at_close.append(operation._stop_requested)
            operation.writer.close()

        failures = await operation.retire_channel(Mock(close=close))
        self.assertEqual(failures, [])
        self.assertEqual(interrupted_at_close, [True])
        await operation.settle()

    async def test_retirement_during_transport_adoption_closes_the_returned_writer(self):
        operation = self.operation()
        entered, release = asyncio.Event(), asyncio.Event()
        original = asyncio.open_connection
        adopted = []

        async def gated(**options):
            result = await original(**options)
            adopted.append(result[1])
            entered.set()
            await release.wait()
            return result

        with patch("nmrnet_provider.native_session.asyncio.open_connection", side_effect=gated):
            caller = asyncio.create_task(operation.start())
            await asyncio.wait_for(entered.wait(), 1)
            retirement = asyncio.create_task(operation.retire())
            await asyncio.sleep(0)
            self.assertTrue(operation._stop_requested)
            self.assertIsNone(operation.writer)
            release.set()
            with self.assertRaisesRegex(Exception, "retired during startup"):
                await caller
            await retirement
            await operation.settle()
        self.assertTrue(adopted[0].is_closing())
        self.assertEqual(list(self.runner.scratch.iterdir()), [])

    async def test_cancelled_science_close_waiter_keeps_the_original_transport_observation(self):
        operation = self.operation()
        await operation.start()
        entered, release = asyncio.Event(), asyncio.Event()
        original = operation.writer.wait_closed

        async def gated():
            entered.set()
            await release.wait()
            await original()

        with patch.object(operation.writer, "wait_closed", side_effect=gated) as observed:
            caller = asyncio.create_task(operation.retire())
            await asyncio.wait_for(entered.wait(), 1)
            retained = operation._science_closure
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertFalse(retained.cancelled())
            release.set()
            await operation.retire()
            await operation.settle()
            self.assertIs(operation._science_closure, retained)
            observed.assert_awaited_once()

    async def test_failed_opening_after_cancellation_reaches_the_diagnostic_recipient(self):
        operation = self.operation()
        entered, release = threading.Event(), threading.Event()
        failure = OSError(5, "injected opening observation failure")
        original = RunnerSession.connect

        def gated(session, **options):
            original(session, **options)
            entered.set()
            if not release.wait(3):
                raise TimeoutError("Test did not release opening observation")
            raise failure

        with patch.object(RunnerSession, "connect", gated):
            caller = asyncio.create_task(operation.start())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            retirement = asyncio.create_task(operation.retire())
            release.set()
            await retirement
            await operation.settle()
        self.notices.assert_called_once()
        self.assertIs(self.notices.call_args.args[0], failure)
        self.assertEqual(self.notices.call_args.args[1]["event"], "native_lifecycle_settled")
        self.assertEqual(list(self.runner.scratch.iterdir()), [])

    def setUp(self):
        self.runner = RunningSupervisor(role="reader")
        self.addCleanup(self.runner.close)

    def operation(self):
        self.notices = Mock()
        return NativeOperation(role="reader", endpoint=self.runner.path, operation_seconds=2, on_notice=self.notices)

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

    async def test_lost_control_waits_for_delayed_runner_readiness_within_original_cleanup_budget(self):
        operation = self.operation()
        await operation.start()
        original = operation.session
        original.request_stop()
        deadline = asyncio.get_running_loop().time() + 1
        while list(self.runner.scratch.iterdir()):
            if asyncio.get_running_loop().time() >= deadline:
                self.fail("Fixture child did not finish before controlled supervisor replacement")
            await asyncio.sleep(0.01)
        self.runner.process.kill()
        await asyncio.to_thread(self.runner.process.wait, timeout=2)
        original.control.close()
        retrying = threading.Event()
        connect = socket.socket.connect

        def observe_refusal(channel, address):
            try:
                return connect(channel, address)
            except OSError as cause:
                if address == str(self.runner.path) and cause.errno in (errno.ENOENT, errno.ECONNREFUSED):
                    retrying.set()
                raise

        with patch.object(socket.socket, "connect", observe_refusal):
            retirement = asyncio.create_task(operation.retire())
            self.assertTrue(await asyncio.to_thread(retrying.wait, 1))
            retained = operation._proof_task
            self.assertFalse(retained.done())
            await asyncio.to_thread(self.runner.restart_with_fresh_scratch)
            await retirement
            await operation.settle()
        self.assertIs(operation._proof_task, retained)
        self.assertNotEqual(operation.proof.generation, original.generation)
        self.assertEqual(operation.proof.lock_identity, original.lock_identity)
        self.assertTrue(operation.proof.retirement.scratch_released)
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
        operation = NativeOperation(role="reader", endpoint=self.runner.path, operation_seconds=0.3, on_notice=Mock())
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
