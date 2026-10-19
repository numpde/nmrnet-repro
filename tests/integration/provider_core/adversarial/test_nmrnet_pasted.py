"""Invalid pasted selections cannot retain an old reference or hide failed cleanup."""

import unittest
from dataclasses import replace
from functools import partial

from tests.integration.nmrnet.pasted_ports import PastedPorts

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import PreparationRetirementFailed
from nmrnet_provider.result import ResultPlan, ResultRejected


class PastedGeometryRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_spans_formats_and_units_allocate_no_worker(self):
        async def choose(ports, change, _instructions, capability):
            options = {"start_byte": ports.start_byte, "end_byte": ports.end_byte, "source_format": "table", "source_unit": "nm"}
            options.update(change)
            await capability.prepare_pasted(**options)

        changes = (
            {"start_byte": -1},
            {"end_byte": 9999},
            {"start_byte": True},
            {"start_byte": 1, "end_byte": 2},
            {"source_format": "json"},
            {"source_format": "sdf"},
            {"source_unit": "unknown"},
        )
        for change in changes:
            with self.subTest(change=change):
                ports = PastedPorts(b"H 0 0 0\n", source_format="table", source_unit="nm")

                owner = ports.owner(partial(choose, ports, change))
                self.addAsyncCleanup(owner.retire)
                with self.assertRaises(InputRejected) as caught:
                    await owner.prepare(ports.instructions)
                self.assertNotIn("Predict the supplied", str(caught.exception))
                ports.allocate_reader.assert_not_awaited()

    async def test_multiple_or_malformed_xyz_never_selects_the_first_frame(self):
        for source in (b"1\nfirst\nH 0 0 0\n1\nsecond\nC 1 0 0\n", b"1\nfirst\nH 0 0 0\n1\nbad\nPRIVATE invalid\n", b"\n"):
            with self.subTest(source=source):
                ports = PastedPorts(source, source_format="xyz", source_unit="nm")
                self.addAsyncCleanup(ports.preparation.retire)
                with self.assertRaises(InputRejected) as caught:
                    await ports.preparation.prepare(ports.instructions)
                self.assertNotIn("PRIVATE", str(caught.exception))
                ports.reader_host.retire.assert_awaited_once()
                self.assertEqual(ports.reader.read.await_args.kwargs["selection"], "all")
                with self.assertRaises(ValueError):
                    ports.preparation.submit("never-issued")

    async def test_failed_replacement_revokes_the_previous_reference(self):
        ports = PastedPorts(b"H 0 0 0\n", source_format="table", source_unit="nm")

        async def choose(_instructions, capability):
            options = {"start_byte": ports.start_byte, "end_byte": ports.end_byte, "source_format": "table", "source_unit": "nm"}
            previous = await capability.prepare_pasted(**options)
            with self.assertRaises(InputRejected):
                await capability.prepare_pasted(**{**options, "end_byte": 9999})
            with self.assertRaisesRegex(ValueError, "reference"):
                capability.submit(previous)
            raise InputRejected("Choose a new valid source span.")

        owner = ports.owner(choose)
        self.addAsyncCleanup(owner.retire)
        with self.assertRaises(InputRejected):
            await owner.prepare(ports.instructions)
        ports.allocate_reader.assert_awaited_once()

    async def test_reader_rejection_keeps_cleanup_failure_as_the_operational_outcome(self):
        ports = PastedPorts(b"PRIVATE invalid\n", source_format="table", source_unit="nm")
        cleanup = OSError("PRIVATE cleanup evidence")
        ports.reader_host.retire.side_effect = cleanup
        with self.assertRaises(PreparationRetirementFailed) as caught:
            await ports.preparation.prepare(ports.instructions)
        self.assertIsInstance(caught.exception.operation_failure, InputRejected)
        self.assertIn(cleanup, [cause for _phase, cause in caught.exception.failures])
        self.assertNotIn("PRIVATE", str(caught.exception))
        with self.assertRaises(ValueError):
            ports.preparation.submit("never-issued")
        ports.reader_host.retire.side_effect = None
        await ports.preparation.retire()

    async def test_result_planner_rejects_mismatched_job_span_source_format_or_unit(self):
        ports = PastedPorts(b"H 0 0 0\n", source_format="table", source_unit="nm")
        self.addAsyncCleanup(ports.preparation.retire)
        prepared = await ports.preparation.prepare(ports.instructions)
        source = prepared.source
        for changes in (
            {"job_ref": "job:other"},
            {"input_fingerprint": "sha256:" + "a" * 64},
            {"start_byte": source.start_byte + 1},
            {"source_bytes": b"C 0 0 0\n"},
            {"source_format": "xyz"},
            {"source_unit": "bohr"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ResultRejected):
                ResultPlan(
                    replace(prepared, source=replace(source, **changes)),
                    job_ref=ports.instructions.job_ref,
                    execution_attempt_ref="attempt:fixture",
                    input_fingerprint=ports.instructions.identity.input_fingerprint,
                    execution_identity={"image_id": "fixture"},
                    selected_artifacts=ports.worker.artifacts,
                    runtime_expectations=ports.worker.runtime,
                )
        self.assertNotIn("H 0 0 0", repr(source))

    async def test_pasted_operations_share_the_existing_worker_call_budget(self):
        ports = PastedPorts(b"H 0 0 0\n", source_format="table", source_unit="nm")

        async def choose(_instructions, capability):
            options = {"start_byte": ports.start_byte, "end_byte": ports.end_byte, "source_format": "table", "source_unit": "nm"}
            previous = await capability.prepare_pasted(**options)
            with self.assertRaisesRegex(RuntimeError, "call limit"):
                await capability.prepare_pasted(**options)
            with self.assertRaisesRegex(ValueError, "reference"):
                capability.submit(previous)
            raise InputRejected("Choose within the allowed worker budget.")

        owner = ports.owner(choose, max_worker_calls=1)
        self.addAsyncCleanup(owner.retire)
        with self.assertRaises(InputRejected):
            await owner.prepare(ports.instructions)
        ports.allocate_reader.assert_awaited_once()
