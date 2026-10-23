"""An unresolved model conversation cannot produce a scientific handle or hide causes."""

import asyncio
import gc
import io
import json
import unittest
import weakref
from builtins import BaseExceptionGroup
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.fakes.execution import unexpected_condition
from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.interpreter_ports import chat_endpoint
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import InputWorkerCallLimit, PreparationRetirementFailed
from nmrnet_provider.interpreter import InputInterpreter
from nmrnet_provider.process import _failure_report
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.result import MAX_RESULT_BYTES, ResultRejected
from nmrnet_provider.session import PreparationEvidenceFailed
from nmrnet_provider.worker_client import WorkerReportedFailure
from provider_core.attempt_record import MAX_EVIDENCE_BYTES
from provider_core.transport import HttpResponseHead
from provider_core.uploads import UploadFailed


class InterpreterPreparationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_foreign_timeout_remains_the_original_operational_failure(self):
        ports = SessionPorts()
        failure = TimeoutError("An independent source operation exceeded its deadline")

        async def choose(_instructions, _capability):
            raise failure

        owner = ports.owner(choose)
        session = ports.make_session(owner)
        self.addAsyncCleanup(session.retire)
        with self.assertRaises(TimeoutError) as caught:
            await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        self.assertIs(caught.exception, failure)
        self.assertEqual(ports.worker.events, [])

    async def test_owned_deadline_cannot_hide_a_late_operational_chooser_failure(self):
        ports = SessionPorts()
        failure = WorkerReportedFailure("Actual worker failure; api_key=PRIVATE_CREDENTIAL")

        async def choose(_instructions, _capability):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise failure from None

        owner = ports.owner(choose, seconds=0.02)
        session = ports.make_session(owner)
        self.addAsyncCleanup(session.retire)
        with self.assertRaises(BaseExceptionGroup) as caught:
            await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        self.assertIn(failure, caught.exception.exceptions)
        rendered = json.dumps(failure_evidence(caught.exception, secrets=()))
        self.assertIn("Actual worker failure", rendered)
        self.assertNotIn("PRIVATE_CREDENTIAL", rendered)
        self.assertEqual(ports.worker.events, [])

    async def test_owned_deadline_cannot_hide_an_operational_cause_carried_by_chooser_cancellation(self):
        for nested in (False, True):
            with self.subTest(nested=nested):
                await self.check_chooser_cancellation_cause(nested)

    async def check_chooser_cancellation_cause(self, nested):
        ports = SessionPorts()
        failure = WorkerReportedFailure("Independent worker failure; api_key=PRIVATE_CREDENTIAL")
        retained = failure
        if nested:
            retained = asyncio.CancelledError()
            retained.__cause__ = failure

        async def choose(_instructions, _capability):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as interruption:
                raise interruption from retained

        owner = ports.owner(choose, seconds=0.02)
        session = ports.make_session(owner)
        self.addAsyncCleanup(session.retire)
        with self.assertRaises(BaseExceptionGroup) as caught:
            await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        self.assertIn(retained, caught.exception.exceptions)
        self.assertIs(owner.chooser_failure.__cause__, retained)
        rendered = json.dumps(failure_evidence(caught.exception, secrets=()))
        self.assertIn("Independent worker failure", rendered)
        self.assertNotIn("PRIVATE_CREDENTIAL", rendered)
        self.assertEqual(ports.worker.events, [])

    async def test_model_fault_observed_during_retirement_cannot_become_a_job_time_limit(self):
        for observation in ("io", "status"):
            with self.subTest(observation=observation):
                await self.check_late_model_fault(observation)

    async def check_late_model_fault(self, observation):
        ports = SessionPorts()
        async with TlsPeer("body-stall", response_headers=((b"x-request-id", b"request:late-model"),)) as peer:
            endpoint = chat_endpoint(peer, exchange_seconds=2)
            original_retire = endpoint.retire

            async def observe_late_fault():
                await original_retire()
                # Model the actual transport retirement contract: it can finish
                # closing while adding late operation evidence to this object.
                exchange = endpoint._transport.last_failure
                if observation == "io":
                    exchange.primary = OSError(5, "PRIVATE_LATE_IO")
                else:
                    exchange.response_head = HttpResponseHead(503, ((b"x-request-id", b"request:late-model"),))

            endpoint.retire = observe_late_fault
            chooser = InputInterpreter((endpoint,), max_turns=2)
            owner = ports.owner(chooser.run, chooser=chooser, seconds=0.1)
            session = ports.make_session(owner)
            try:
                with self.assertRaises(BaseExceptionGroup) as caught:
                    await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
                rendered = json.dumps(failure_evidence(caught.exception, secrets=("PRIVATE_MODEL_KEY",)))
                self.assertIn("request:late-model", rendered)
                self.assertIn('"errno": 5' if observation == "io" else '"status": 503', rendered)
                self.assertNotIn("PRIVATE_MODEL_KEY", rendered)
                self.assertNotIn("PRIVATE_LATE_IO", rendered)
                self.assertEqual(len(peer.requests), 1)
                self.assertEqual(ports.worker.events, [])
            finally:
                await session.retire()

    async def test_owned_deadline_cannot_hide_source_failure_settled_after_chooser_cancellation(self):
        ports = SessionPorts()
        failure = OSError(5, "PRIVATE_SOURCE_FAILURE")

        async def inspect():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise failure from None

        ports.uploads.inspect.side_effect = inspect

        async def choose(_instructions, capability):
            await capability.inspect()

        owner = ports.owner(choose, seconds=0.02)
        session = ports.make_session(owner)
        self.addAsyncCleanup(session.retire)
        with self.assertRaises(BaseExceptionGroup) as caught:
            await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        self.assertIs(owner.source_failure, failure)
        self.assertIn(failure, caught.exception.exceptions)
        self.assertEqual(ports.worker.events, [])

    async def test_owned_deadline_with_failed_cleanup_remains_operational(self):
        ports = SessionPorts()
        cleanup = OSError(5, "PRIVATE_CLEANUP")

        async def choose(_instructions, _capability):
            await asyncio.Event().wait()

        chooser = SimpleNamespace(run=choose, retire=AsyncMock(side_effect=cleanup))
        owner = ports.owner(choose, chooser=chooser, seconds=0.02)
        session = ports.make_session(owner)
        try:
            with self.assertRaises(PreparationRetirementFailed) as caught:
                await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
            self.assertIsInstance(caught.exception.operation_failure, TimeoutError)
            self.assertIs(caught.exception.failures[0][1], cleanup)
            self.assertEqual(ports.worker.events, [])
        finally:
            chooser.retire.side_effect = None
            await session.retire()

    async def test_source_cancellation_keeps_its_owned_operational_cause_out_of_job_limit_classification(self):
        ports = SessionPorts()
        outage = UploadFailed("the source session's cumulative deadline expired", cause=TimeoutError())

        async def inspect():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as interruption:
                raise interruption from outage

        ports.uploads.inspect.side_effect = inspect

        async def choose(_instructions, capability):
            await capability.inspect()

        owner = ports.owner(choose, seconds=0.02)
        session = ports.make_session(owner)
        self.addAsyncCleanup(session.retire)
        with self.assertRaises(BaseExceptionGroup) as caught:
            await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        self.assertIsInstance(owner.source_failure, asyncio.CancelledError)
        self.assertIs(owner.source_failure.__cause__, outage)
        self.assertIn(outage, caught.exception.exceptions)
        self.assertIn("cumulative deadline", json.dumps(failure_evidence(caught.exception, secrets=())))
        self.assertEqual(ports.worker.events, [])

    async def test_external_cancellation_during_deadline_retirement_stays_the_same_cancellation(self):
        ports = SessionPorts()
        retiring = asyncio.Event()
        interruptions = []

        async def choose(_instructions, _capability):
            await asyncio.Event().wait()

        async def held_retirement():
            retiring.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as interruption:
                interruptions.append(interruption)
                raise

        ports.transports[0].retire.side_effect = held_retirement
        owner = ports.owner(choose, seconds=0.02)
        session = ports.make_session(owner)
        task = asyncio.create_task(session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition))
        try:
            await asyncio.wait_for(retiring.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
            self.assertIs(caught.exception, interruptions[0])
            self.assertIsInstance(caught.exception.__cause__, PreparationRetirementFailed)
            self.assertEqual(ports.worker.events, [])
        finally:
            ports.transports[0].retire.side_effect = None
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await session.retire()

    async def test_call_limit_cannot_hide_failed_chooser_retirement_or_become_a_public_job_failure(self):
        ports = SessionPorts()
        ports.reader.read.side_effect = InputRejected("Cannot read selected coordinate table.")

        async def choose(_instructions, capability):
            with self.assertRaises(InputRejected):
                await capability.prepare_pasted(source_text="H invalid 0 0\n", source_format="table", source_unit="angstrom")
            await capability.prepare_smiles(smiles="CCl", requested_count=2)

        cleanup = OSError("PRIVATE_MODEL_KEY private_key=PRIVATE_CREDENTIAL")
        chooser = SimpleNamespace(run=choose, retire=AsyncMock(side_effect=cleanup))
        owner = ports.owner(choose, chooser=chooser, max_worker_calls=1)
        session = ports.make_session(owner)
        with self.assertRaises(PreparationRetirementFailed) as caught:
            await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        self.assertIsInstance(caught.exception.operation_failure, InputWorkerCallLimit)
        self.assertIs(caught.exception.failures[0][1], cleanup)
        protected = json.dumps(failure_evidence(caught.exception, secrets=("PRIVATE_MODEL_KEY",)))
        self.assertIn("maximum_calls", protected)
        self.assertIn("calls_started", protected)
        self.assertIn("operator", protected)
        self.assertNotIn("PRIVATE_MODEL_KEY", protected)
        self.assertNotIn("PRIVATE_CREDENTIAL", protected)
        self.assertEqual(ports.worker.events, [])
        ports.allocate_reader.assert_awaited_once()
        ports.allocate_generator.assert_not_awaited()
        chooser.retire.side_effect = None
        await session.retire()

    async def test_invalid_output_on_each_capability_has_no_source_or_allocation_effect(self):
        ports = SessionPorts()

        async def choose(_instructions, capability):
            for output in (
                None,
                [],
                {"unknown": "PRIVATE_FIELD"},
                {"verbosity": None},
                {"verbosity": True},
                {"verbosity": "compact"},
                {"verbosity": "full"},
                {"verbosity": "LOW"},
                {"shift_significant_digits": None},
                {"shift_significant_digits": True},
                {"shift_significant_digits": 0},
                {"shift_significant_digits": 18},
            ):
                for operation, arguments in (
                    (capability.prepare_smiles, {"smiles": "CCl", "requested_count": 2}),
                    (
                        capability.prepare_pasted,
                        {"source_text": "H 0 0 0\n", "source_format": "table", "source_unit": "angstrom"},
                    ),
                    (
                        capability.prepare_upload,
                        {"upload_ref": "upload:unreached", "source_format": "xyz", "source_unit": "angstrom", "selection": "all"},
                    ),
                ):
                    with (
                        self.subTest(output=output, operation=operation.__name__),
                        self.assertRaisesRegex(InputRejected, "output|verbosity|shift_significant_digits"),
                    ):
                        await operation(**arguments, output=output)
            ports.allocate_reader.assert_not_awaited()
            ports.allocate_generator.assert_not_awaited()
            ports.uploads.acquire.assert_not_awaited()
            with self.assertRaises(InputRejected):
                await capability.prepare_smiles(smiles="CCl", requested_count=0, output={"shift_significant_digits": 3})
            await capability.prepare_smiles(smiles="CCl", requested_count=2)

        owner = ports.owner(choose)
        self.addAsyncCleanup(owner.retire)
        prepared = await owner.prepare(ports.instructions)
        self.assertEqual(prepared.output.as_dict(), {"verbosity": "medium", "shift_significant_digits": 4})
        ports.generator.generate.assert_awaited_once_with(b"CCl", 2)

    async def test_parent_cancellation_retains_the_original_late_operational_child_failure_after_clean_join(self):
        ports = SessionPorts()
        entered, child_cancelled, release = (asyncio.Event() for _ in range(3))
        failure = WorkerReportedFailure("valence rejected by the worker; api_key=PRIVATE_CREDENTIAL")

        async def choose(_instructions, _capability):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                child_cancelled.set()
                await release.wait()
            raise failure

        chooser = SimpleNamespace(run=choose, retire=AsyncMock())
        owner = ports.owner(choose, chooser=chooser)
        task = asyncio.create_task(owner.prepare(ports.instructions))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.wait_for(child_cancelled.wait(), 1)
            release.set()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
            self.assertIs(owner.chooser_failure, failure)
            self.assertIs(caught.exception.__cause__, failure)
            self.assertIsNone(owner.last_retirement_failure)
            chooser.retire.assert_awaited_once()
            self.assertEqual(ports.worker.events, [])
            await owner.retire()
            output = io.StringIO()
            with (
                redirect_stderr(output),
                patch("nmrnet_provider.process._write_stderr", side_effect=lambda raw: output.write(raw.decode("ascii"))),
            ):
                _failure_report((("input preparation interrupted", caught.exception),), secrets=(), held=False)
            report = output.getvalue()
            self.assertIn("WorkerReportedFailure", report)
            self.assertIn("valence rejected by the worker", report)
            self.assertNotIn("PRIVATE_CREDENTIAL", report)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await owner.retire()

    async def choose(self, observation, _instructions, capability):
        await capability.prepare_smiles(smiles="CCl", requested_count=2)

        return observation

    async def test_retirement_failure_after_selection_blocks_the_handle(self):
        ports = SessionPorts()
        cleanup = OSError("PRIVATE model cleanup")

        async def choose(instructions, capability):
            return await self.choose(b"{}", instructions, capability)

        chooser = SimpleNamespace(run=choose, retire=AsyncMock(side_effect=cleanup))
        preparation = ports.owner(choose, chooser=chooser)
        session = ports.make_session(preparation)
        with self.assertRaises(PreparationRetirementFailed) as caught:
            await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
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

    async def test_retirement_after_selection_blocks_a_late_candidate_during_model_cleanup(self):
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
            await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
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
            await session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
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
