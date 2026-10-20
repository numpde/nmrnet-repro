"""Adversarial preparation contracts with controlled source and reader ports.

Real XYZ parsing and immutable verified Upload values are used here. The separate
Upload session fixture covers actual TLS acquisition and durable publication.
"""

import asyncio
import hashlib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tests.fakes.job_input import facts, response
from tests.fakes.uploads import UPLOAD_REF

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import InputPreparation, PreparationRetirementFailed
from nmrnet_provider.smiles import GenerationSettings
from nmrnet_provider.xyz import prepare_xyz
from provider_core.job_input import verify_job_input
from provider_core.uploads import VerifiedUpload


class PreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = Path("/fixtures/mixed_records.xyz").read_bytes()
        self.verified = VerifiedUpload(
            "job:example", UPLOAD_REF, len(self.source), "sha256:" + hashlib.sha256(self.source).hexdigest(), self.source
        )
        self.uploads = SimpleNamespace(job_ref="job:example", inspect=AsyncMock(), acquire=AsyncMock(return_value=self.verified))
        self.transports = (SimpleNamespace(retire=AsyncMock()), SimpleNamespace(retire=AsyncMock()))
        self.readers = []
        self.instructions = verify_job_input(response(), feed=facts())

    async def allocate(self):
        async def read(source, **options):
            self.assertIs(source, self.source)
            options.pop("source_format")
            options.pop("alternate_location")
            return prepare_xyz(source, **options)

        reader = SimpleNamespace(read=read, close=Mock(), wait_closed=AsyncMock())
        host = SimpleNamespace(retire=AsyncMock())
        self.readers.append((reader, host))
        return reader, host

    def owner(self, script, *, reads=2, seconds=1, allocator=None):
        return InputPreparation(
            uploads=self.uploads,
            source_api_transport=self.transports[0],
            source_store_transport=self.transports[1],
            allocate_reader=self.allocate if allocator is None else allocator,
            allocate_generator=AsyncMock(side_effect=AssertionError("Upload fixture unexpectedly allocated a generator.")),
            generation_settings=GenerationSettings(17, 200, 1),
            generation_seconds=2,
            chooser=SimpleNamespace(run=script, retire=AsyncMock()),
            max_worker_calls=reads,
            seconds=seconds,
        )

    async def test_selection_correction_reuses_exact_verified_bytes_and_retires_before_replacement(self):
        async def choose(_instructions, capability):
            await capability.inspect()
            with self.assertRaises(InputRejected):
                await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(99,))
            self.readers[0][1].retire.assert_awaited_once()
            self.readers[0][0].wait_closed.assert_awaited_once()
            token = await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(2, 1))
            capability.submit(token)

        owner = self.owner(choose)
        prepared = await owner.prepare(self.instructions)
        self.assertEqual([geometry.source_record_number for geometry in prepared.geometries], [2, 1])
        self.uploads.acquire.assert_awaited_once_with(UPLOAD_REF)
        self.assertEqual(len(self.readers), 2)
        await owner.retire()

    async def test_unknown_reference_and_repeated_submit_cannot_replace_frozen_input(self):
        async def choose(_instructions, capability):
            await capability.inspect()
            token = await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(2, 1))
            with self.assertRaisesRegex(ValueError, "reference"):
                capability.submit("forged-reference")
            capability.submit(token)
            with self.assertRaisesRegex(ValueError, "submitted"):
                capability.submit(token)
            with self.assertRaisesRegex(ValueError, "submitted"):
                await capability.inspect()

        owner = self.owner(choose)
        prepared = await owner.prepare(self.instructions)
        self.assertEqual(len(prepared.geometries), 2)
        with self.assertRaises(ValueError):
            await owner.prepare(self.instructions)
        await owner.retire()

    async def test_invalid_options_and_exhausted_read_budget_never_acquire_another_source(self):
        async def choose(_instructions, capability):
            with self.assertRaises(InputRejected):
                await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="guessed", selection=(1,))
            self.uploads.acquire.assert_not_awaited()
            token = await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))
            with self.assertRaisesRegex(RuntimeError, "call limit"):
                await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(2,))
            with self.assertRaisesRegex(ValueError, "reference"):
                capability.submit(token)

        owner = self.owner(choose, reads=1)
        with self.assertRaisesRegex(ValueError, "submit"):
            await owner.prepare(self.instructions)
        self.uploads.acquire.assert_awaited_once()
        self.assertEqual(len(self.readers), 1)
        await owner.retire()

    async def test_acquisition_failure_does_not_consume_a_parse_attempt(self):
        self.uploads.acquire.side_effect = (RuntimeError("controlled acquisition failure"), self.verified)

        async def choose(_instructions, capability):
            with self.assertRaisesRegex(RuntimeError, "acquisition"):
                await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))
            token = await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(2,))
            capability.submit(token)

        owner = self.owner(choose, reads=1)
        prepared = await owner.prepare(self.instructions)
        self.assertEqual(prepared.geometries[0].source_record_number, 2)
        self.assertEqual(len(self.readers), 1)
        await owner.retire()

    async def test_failed_selection_replacement_revokes_the_previous_reference(self):
        async def choose(_instructions, capability):
            token = await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))
            with self.assertRaises(InputRejected):
                await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(99,))
            with self.assertRaisesRegex(ValueError, "reference"):
                capability.submit(token)

        owner = self.owner(choose)
        with self.assertRaisesRegex(ValueError, "submit"):
            await owner.prepare(self.instructions)
        await owner.retire()

    async def test_read_rejection_and_failed_cleanup_keep_both_private_causes(self):
        rejection = InputRejected(
            "Cannot read selected geometry: its atom block is incomplete.", diagnostic="private native evidence"
        )
        cleanup = OSError("private host evidence")

        async def allocate():
            reader, host = await self.allocate()
            reader.read = AsyncMock(side_effect=rejection)
            host.retire.side_effect = cleanup
            return reader, host

        async def choose(_instructions, capability):
            await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))

        owner = self.owner(choose, allocator=allocate)
        with self.assertRaises(PreparationRetirementFailed) as caught:
            await owner.prepare(self.instructions)
        self.assertIs(caught.exception.operation_failure, rejection)
        self.assertIs(caught.exception.cause, rejection)
        self.assertEqual(caught.exception.operation_failure.diagnostic, "private native evidence")
        self.assertIs(caught.exception.failures[0][1], cleanup)
        self.assertNotIn(rejection.diagnostic, str(caught.exception))
        self.assertNotIn(str(cleanup), str(caught.exception))
        self.readers[0][1].retire.side_effect = None
        await owner.retire()

    async def test_uncertain_reader_retirement_blocks_replacement_and_preserves_owner(self):
        original_allocate = self.allocate
        failure = OSError("private exact-allocation cleanup evidence")

        async def allocate():
            reader, host = await original_allocate()
            host.retire.side_effect = failure
            return reader, host

        async def choose(_instructions, capability):
            with self.assertRaises(PreparationRetirementFailed) as caught:
                await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))
            self.assertIs(caught.exception.failures[0][1], failure)
            with self.assertRaisesRegex(RuntimeError, "retirement"):
                await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(2,))

        owner = self.owner(choose, allocator=allocate)
        with self.assertRaisesRegex(ValueError, "submit"):
            await owner.prepare(self.instructions)
        with self.assertRaises(PreparationRetirementFailed):
            await owner.retire()
        for transport in self.transports:
            transport.retire.assert_awaited_once()
        self.assertEqual(len(self.readers), 1)
        self.readers[0][1].retire.side_effect = None
        await owner.retire()

    async def test_preparation_timeout_keeps_source_cleanup_owned(self):
        async def choose(_instructions, capability):
            await capability.inspect()
            await asyncio.Future()

        owner = self.owner(choose, seconds=0.02)
        with self.assertRaises(TimeoutError):
            await owner.prepare(self.instructions)
        await owner.retire()
        for transport in self.transports:
            transport.retire.assert_awaited_once()

    async def test_both_source_cleanup_failures_are_retained_with_their_owners(self):
        first, second = OSError("API closure canary"), OSError("store closure canary")
        self.transports[0].retire.side_effect = first
        self.transports[1].retire.side_effect = second
        owner = self.owner(AsyncMock())
        with self.assertRaises(PreparationRetirementFailed) as caught:
            await owner.retire()
        self.assertEqual(caught.exception.failures, (("source_api_retirement", first), ("source_store_retirement", second)))
        for transport in self.transports:
            transport.retire.assert_awaited_once()
            transport.retire.side_effect = None
        await owner.retire()

    async def test_cleanup_cancellation_retains_the_original_reader_rejection(self):
        rejection = InputRejected("Cannot read selected record: missing atoms.", diagnostic="private reader canary")
        cancelled = asyncio.CancelledError()

        async def allocate():
            reader, host = await self.allocate()
            reader.read = AsyncMock(side_effect=rejection)
            host.retire.side_effect = cancelled
            return reader, host

        async def choose(_instructions, capability):
            await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))

        owner = self.owner(choose, allocator=allocate)
        with self.assertRaises(asyncio.CancelledError) as caught:
            await owner.prepare(self.instructions)
        self.assertIs(caught.exception, cancelled)
        self.assertIs(caught.exception.__cause__.operation_failure, rejection)
        self.readers[0][1].retire.side_effect = None
        await owner.retire()

    async def test_cancelled_allocation_is_retained_until_late_reader_is_retired(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def allocate():
            entered.set()
            await release.wait()
            return await self.allocate()

        async def choose(_instructions, capability):
            await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))

        owner = self.owner(choose, allocator=allocate)
        task = asyncio.create_task(owner.prepare(self.instructions))
        await entered.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.readers[0][1].retire.assert_awaited_once()
        self.readers[0][0].wait_closed.assert_awaited_once()
        await owner.retire()

    async def test_retirement_settles_acquisition_before_closing_its_transports(self):
        entered, release, settled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        later_exchange = AsyncMock()

        async def acquire(_reference):
            entered.set()
            try:
                await release.wait()
                await later_exchange()
                return self.verified
            finally:
                settled.set()

        async def transport_retire():
            self.assertTrue(settled.is_set(), "Source transports closed while acquisition could start another exchange")

        self.uploads.acquire.side_effect = acquire
        for transport in self.transports:
            transport.retire.side_effect = transport_retire

        async def choose(_instructions, capability):
            await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))

        owner = self.owner(choose)
        preparing = asyncio.create_task(owner.prepare(self.instructions))
        await entered.wait()
        await owner.retire()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await preparing
        later_exchange.assert_not_awaited()
        self.assertEqual(self.readers, [])
        with self.assertRaisesRegex(ValueError, "retiring"):
            await owner.inspect()
