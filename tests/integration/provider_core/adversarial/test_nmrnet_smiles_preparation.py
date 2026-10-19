"""No reference survives invalid selection, generation failure or uncertain cleanup."""

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.smiles_ports import InputPorts

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import PreparationRetirementFailed
from nmrnet_provider.smiles import ConformerShortfall


class SmilesPreparationRejectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ports = InputPorts()

    async def test_invalid_span_revokes_previous_selection_without_allocating_generator(self):
        async def choose(_instructions, capability):
            previous = await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))
            with self.assertRaises(InputRejected):
                await capability.prepare_smiles(start_byte=7, end_byte=1000, requested_count=2)
            with self.assertRaisesRegex(ValueError, "reference"):
                capability.submit(previous)

        owner = self.ports.owner(choose)
        with self.assertRaisesRegex(ValueError, "submit"):
            await owner.prepare(self.ports.instructions)
        self.ports.allocate_generator.assert_not_awaited()
        await owner.retire()

    async def test_upload_and_generation_cannot_reset_the_shared_work_limit(self):
        async def choose(_instructions, capability):
            await capability.prepare_smiles(start_byte=7, end_byte=10, requested_count=2)
            with self.assertRaisesRegex(RuntimeError, "call limit"):
                await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))

        owner = self.ports.owner(choose, max_worker_calls=1)
        with self.assertRaisesRegex(ValueError, "submit"):
            await owner.prepare(self.ports.instructions)
        self.ports.uploads.acquire.assert_not_awaited()
        self.ports.allocate_reader.assert_not_awaited()
        await owner.retire()

    async def test_shortfall_retains_typed_failure_and_confirms_retirement_without_submission(self):
        partial = replace(self.ports.batch, conformers=self.ports.batch.conformers[:1])
        failure = ConformerShortfall(partial)
        self.ports.generator.generate.side_effect = failure

        async def choose(_instructions, capability):
            await capability.prepare_smiles(start_byte=7, end_byte=10, requested_count=2)

        owner = self.ports.owner(choose)
        with self.assertRaises(ConformerShortfall) as caught:
            await owner.prepare(self.ports.instructions)
        self.assertIs(caught.exception, failure)
        self.ports.generator_host.retire.assert_awaited_once()
        self.ports.generator.wait_closed.assert_awaited_once()
        await owner.retire()

    async def test_generation_and_cleanup_failure_preserve_both_private_causes(self):
        failure = InputRejected("Cannot generate conformers: invalid selected molecular graph.", diagnostic="PRIVATE_PARSE")
        cleanup = OSError("PRIVATE_HOST_RETIREMENT")
        self.ports.generator.generate.side_effect = failure
        self.ports.generator_host.retire.side_effect = cleanup

        async def choose(_instructions, capability):
            await capability.prepare_smiles(start_byte=7, end_byte=10, requested_count=2)

        owner = self.ports.owner(choose)
        with self.assertRaises(PreparationRetirementFailed) as caught:
            await owner.prepare(self.ports.instructions)
        self.assertIs(caught.exception.operation_failure, failure)
        self.assertIs(caught.exception.failures[0][1], cleanup)
        self.assertNotIn("PRIVATE_", str(caught.exception))
        self.ports.generator_host.retire.side_effect = None
        await owner.retire()

    async def test_unconfirmed_worker_retirement_precedes_malformed_choices_on_every_route(self):
        cleanup = OSError("PRIVATE_HOST_RETIREMENT")
        self.ports.generator_host.retire.side_effect = cleanup

        async def choose(_instructions, capability):
            with self.assertRaises(PreparationRetirementFailed):
                await capability.prepare_smiles(start_byte=7, end_byte=10, requested_count=2)
            with self.assertRaisesRegex(RuntimeError, "retirement"):
                await capability.prepare_smiles(start_byte=-1, end_byte=10, requested_count=2)
            with self.assertRaisesRegex(RuntimeError, "retirement"):
                await capability.prepare_upload(UPLOAD_REF, source_format="unsupported", source_unit="angstrom", selection=(1,))

        owner = self.ports.owner(choose)
        with self.assertRaisesRegex(ValueError, "submit"):
            await owner.prepare(self.ports.instructions)
        self.ports.allocate_generator.assert_awaited_once()
        self.ports.allocate_reader.assert_not_awaited()
        self.ports.uploads.acquire.assert_not_awaited()
        self.ports.generator_host.retire.side_effect = None
        await owner.retire()

    async def test_cancelled_generator_allocation_stays_owned_until_late_retirement(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def allocate(**_settings):
            entered.set()
            await release.wait()
            return self.ports.generator, self.ports.generator_host

        self.ports.allocate_generator.side_effect = allocate

        async def choose(_instructions, capability):
            await capability.prepare_smiles(start_byte=7, end_byte=10, requested_count=2)

        owner = self.ports.owner(choose)
        task = asyncio.create_task(owner.prepare(self.ports.instructions))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.ports.generator.generate.assert_not_awaited()
        self.ports.generator_host.retire.assert_awaited_once()
        self.ports.generator.wait_closed.assert_awaited_once()
        await owner.retire()

    async def test_cancellation_after_generation_starts_retires_without_a_reference(self):
        entered = asyncio.Event()

        async def generate(_source, _count):
            entered.set()
            await asyncio.Future()

        self.ports.generator.generate.side_effect = generate

        async def choose(_instructions, capability):
            await capability.prepare_smiles(start_byte=7, end_byte=10, requested_count=2)

        owner = self.ports.owner(choose)
        task = asyncio.create_task(owner.prepare(self.ports.instructions))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.ports.generator.close.assert_called_once()
        self.ports.generator_host.retire.assert_awaited_once()
        self.ports.generator.wait_closed.assert_awaited_once()
        with self.assertRaisesRegex(ValueError, "not active"):
            owner.submit("never-issued")
        await owner.retire()

    async def test_incompatible_native_deadline_is_rejected_before_allocation(self):
        with self.assertRaisesRegex(ValueError, "deadline"):
            self.ports.owner(AsyncMock(), generation_seconds=1)
        self.ports.allocate_generator.assert_not_awaited()
