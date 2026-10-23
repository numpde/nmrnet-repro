"""Archive inspection cannot reset the existing per-Attempt reader-call limit."""

import unittest
from unittest.mock import AsyncMock

from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.interpreter_ports import UploadPorts

from nmrnet_provider.input_preparation import InputWorkerCallLimit


class UploadPreparationBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspection_consumes_the_existing_worker_budget_before_preparation(self):
        ports = UploadPorts()
        ports.reader.inspect_upload = AsyncMock(return_value={"container": "direct", "members": []})

        async def choose(_instructions, capability):
            await capability.inspect_upload(UPLOAD_REF)
            ports.reader_host.retire.assert_awaited_once()
            with self.assertRaises(InputWorkerCallLimit):
                await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(1,))

        owner = ports.owner(choose, max_worker_calls=1)
        self.addAsyncCleanup(owner.retire)
        with self.assertRaisesRegex(ValueError, "select"):
            await owner.prepare(ports.instructions)
        ports.uploads.acquire.assert_awaited_once_with(UPLOAD_REF)
        ports.allocate_reader.assert_awaited_once()
