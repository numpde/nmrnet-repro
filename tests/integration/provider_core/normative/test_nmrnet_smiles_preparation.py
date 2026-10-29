"""One chooser owns supplied and generated inputs and their worker retirement."""

import asyncio
import unittest

from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.smiles_ports import InputPorts

from nmrnet_provider.prepared_input import GeneratedJobInput


class SmilesPreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ports = InputPorts()

    async def test_exact_job_selection_and_fixed_settings_reach_one_retired_generator(self):
        async def choose(instructions, capability):
            self.assertIs(instructions, self.ports.instructions)
            await capability.prepare_smiles(smiles="CCl", requested_count=2)
            self.ports.generator_host.retire.assert_awaited_once_with()
            self.ports.generator.wait_closed.assert_awaited_once_with()

        owner = self.ports.owner(choose)
        prepared = await owner.prepare(self.ports.instructions, require_execution=lambda: None)
        self.assertIs(type(prepared), GeneratedJobInput)
        self.assertIs(prepared.generated, self.ports.batch)
        self.assertEqual(prepared.source.source_bytes, b"CCl")
        self.ports.generator.generate.assert_awaited_once_with(b"CCl", 2)
        self.ports.allocate_generator.assert_awaited_once_with(settings=self.ports.settings, operation_seconds=2)
        self.ports.allocate_reader.assert_not_awaited()
        self.ports.uploads.acquire.assert_not_awaited()
        with self.assertRaisesRegex(ValueError, "not active"):
            await owner.inspect()
        await owner.retire()
        self.ports.generator.generate.assert_awaited_once()

    async def test_success_is_terminal_and_cannot_allocate_another_route(self):
        async def choose(_instructions, capability):
            await capability.prepare_smiles(smiles="CCl", requested_count=2)
            with self.assertRaisesRegex(ValueError, "not active"):
                await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))
            with self.assertRaisesRegex(ValueError, "not active"):
                await capability.prepare_smiles(smiles="CCl", requested_count=2)

        owner = self.ports.owner(choose)
        prepared = await owner.prepare(self.ports.instructions, require_execution=lambda: None)
        self.assertIs(prepared.generated, self.ports.batch)
        self.ports.allocate_generator.assert_awaited_once()
        self.ports.allocate_reader.assert_not_awaited()
        await owner.retire()

    async def test_candidate_remains_unavailable_until_host_retirement_finishes(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def retire():
            entered.set()
            await release.wait()

        self.ports.generator_host.retire.side_effect = retire

        async def choose(_instructions, capability):
            await capability.prepare_smiles(smiles="CCl", requested_count=2)

        owner = self.ports.owner(choose)
        task = asyncio.create_task(owner.prepare(self.ports.instructions, require_execution=lambda: None))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            self.assertFalse(task.done())
            with self.assertRaisesRegex(ValueError, "another source operation"):
                await owner.inspect()
        finally:
            release.set()
            await asyncio.wait_for(task, timeout=1)
            await owner.retire()
