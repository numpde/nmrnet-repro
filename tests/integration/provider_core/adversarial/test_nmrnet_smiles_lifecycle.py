"""The fixed proof owns prestarted grants even before its chooser allocates clients."""

import asyncio
import unittest
from builtins import ExceptionGroup
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.integration.nmrnet.normative.test_nmrnet_smiles_lifecycle import (
    PrestartedGeneratorInput,
    require_host_source_closure,
)

from nmrnet_provider.input_preparation import InputPreparation
from nmrnet_provider.smiles import GenerationSettings


class PrestartedGeneratorCleanupTests(unittest.IsolatedAsyncioTestCase):
    def test_mounted_sources_require_matching_host_manifest_entries(self):
        execution = {"tool_sources": {"tools/example.py": {"sha256": "a" * 64}}}
        matching = ("a" * 64 + "  tools/example.py\n").encode()
        require_host_source_closure(execution, matching)
        for manifest in (b"", ("b" * 64 + "  tools/example.py\n").encode()):
            with self.subTest(manifest=manifest), self.assertRaisesRegex(AssertionError, "tools/example.py"):
                require_host_source_closure(execution, manifest)

    def setUp(self):
        self.host = SimpleNamespace(retire=AsyncMock())
        self.transports = tuple(SimpleNamespace(retire=AsyncMock()) for _ in range(2))
        self.allocate = AsyncMock()
        self.owner = PrestartedGeneratorInput(
            generator_host=self.host,
            uploads=SimpleNamespace(job_ref="job:example"),
            source_api_transport=self.transports[0],
            source_store_transport=self.transports[1],
            allocate_reader=self.allocate,
            allocate_generator=self.allocate,
            generation_settings=GenerationSettings(17, 200, 1),
            generation_seconds=2,
            chooser=SimpleNamespace(run=AsyncMock(), retire=AsyncMock()),
            max_worker_calls=1,
            seconds=1,
        )

    async def test_unused_prestarted_endpoint_is_retired_without_allocating_a_client(self):
        await self.owner.retire()
        self.allocate.assert_not_awaited()
        for transport in self.transports:
            transport.retire.assert_awaited_once()
        self.host.retire.assert_awaited_once()

    async def test_input_and_host_cleanup_failures_are_both_retained(self):
        first, second = OSError("private input cleanup"), OSError("private generator cleanup")
        self.host.retire.side_effect = second
        with (
            patch.object(InputPreparation, "retire", new=AsyncMock(side_effect=first)) as normal,
            self.assertRaises(ExceptionGroup) as caught,
        ):
            await self.owner.retire()
        normal.assert_awaited_once()
        self.host.retire.assert_awaited_once()
        self.assertEqual(caught.exception.exceptions, (first, second))

    async def test_input_cancellation_preserves_later_host_failure_and_stays_cancellation(self):
        cancelled, cleanup = asyncio.CancelledError(), OSError("private generator cleanup")
        self.host.retire.side_effect = cleanup
        with (
            patch.object(InputPreparation, "retire", new=AsyncMock(side_effect=cancelled)),
            self.assertRaises(asyncio.CancelledError) as caught,
        ):
            await self.owner.retire()
        self.assertIs(caught.exception, cancelled)
        self.assertEqual(caught.exception.__cause__.exceptions, (cancelled, cleanup))
        self.host.retire.assert_awaited_once()
