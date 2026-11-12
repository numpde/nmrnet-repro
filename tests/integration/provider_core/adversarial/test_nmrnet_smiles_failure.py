"""Generation failures propagate unchanged without admitting partial science."""

import asyncio
import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from tests.fakes.execution import unexpected_condition
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.input_preparation import PreparationRetirementFailed
from nmrnet_provider.smiles import ConformerShortfall
from nmrnet_provider.smiles_protocol import ConformerOutputLimit


def output_limit(batch):
    """Author the compact port failure; real frame-limit tests live separately."""
    reply = {
        "kind": "conformer_output_limit",
        "allocation": "PRIVATE_ALLOCATION",
        "coordinates_available": False,
        "provenance_utf8": batch.provenance.decode(),
        "diagnostic": batch.diagnostic,
        "conformers": [
            {
                "conformer_id": item.conformer_id,
                "request_sha256": hashlib.sha256(item.request_bytes).hexdigest(),
                "request_byte_length": len(item.request_bytes),
                "stereochemistry_sha256": hashlib.sha256(item.stereochemistry).hexdigest(),
                "stereochemistry_byte_length": len(item.stereochemistry),
            }
            for item in batch.conformers
        ],
    }
    return ConformerOutputLimit(reply, 2)


class SmilesFailureTests(unittest.IsolatedAsyncioTestCase):
    async def run_failure(self, ports, failure):
        self.addAsyncCleanup(ports.session.retire)
        ports.generator.generate.side_effect = failure
        with self.assertRaises(type(failure)) as caught:
            await ports.session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        self.assertIs(caught.exception, failure)
        self.assertEqual([], ports.worker.events)
        self.assertFalse(ports.host.confirmed)
        ports.generator.close.assert_called_once_with()
        ports.generator_host.retire.assert_awaited_once()
        ports.generator.wait_closed.assert_awaited_once()
        with self.assertRaises(ValueError):
            await ports.session.execute(object(), report_condition=unexpected_condition)
        with self.assertRaises(ValueError):
            await ports.session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)

    async def test_shortfall_preserves_original_partial_coordinates_and_evidence(self):
        ports = SessionPorts()
        partial = replace(ports.batch, conformers=ports.batch.conformers[:1])
        failure = ConformerShortfall(partial)
        await self.run_failure(ports, failure)
        self.assertIs(failure.partial, partial)
        self.assertIs(failure.partial.conformers[0], ports.batch.conformers[0])
        self.assertEqual([item.conformer_id for item in failure.partial.conformers], [2])
        self.assertIs(failure.partial.provenance, ports.batch.provenance)
        self.assertEqual(failure.partial.diagnostic, ports.batch.diagnostic)

    async def test_output_limit_preserves_original_compact_diagnostic(self):
        ports = SessionPorts()
        failure = output_limit(ports.batch)
        original_diagnostic = failure.diagnostic
        await self.run_failure(ports, failure)
        self.assertIs(failure.diagnostic, original_diagnostic)
        self.assertEqual(2, failure.requested_count)
        self.assertEqual(2, failure.actual_count)
        generation = json.loads(failure.diagnostic)
        self.assertEqual("PRIVATE_ALLOCATION", generation["allocation"])
        self.assertEqual(ports.batch.provenance.decode(), generation["provenance_utf8"])
        self.assertEqual(ports.batch.diagnostic, generation["diagnostic"])
        self.assertIs(generation["coordinates_available"], False)
        self.assertEqual([item["conformer_id"] for item in generation["conformers"]], [2, 0])

    async def test_zero_returned_conformers_remain_shortfall_with_no_fabricated_payload_identities(self):
        ports = SessionPorts()
        partial = replace(ports.batch, conformers=())
        failure = ConformerShortfall(partial)
        await self.run_failure(ports, failure)
        self.assertIs(failure.partial, partial)
        self.assertEqual((), failure.partial.conformers)

    async def test_large_partial_diagnostic_propagates_without_evidence_admission(self):
        ports = SessionPorts()
        partial = replace(ports.batch, conformers=ports.batch.conformers[:1], diagnostic="PRIVATE" * 40000)
        failure = ConformerShortfall(partial)
        await self.run_failure(ports, failure)
        self.assertIs(failure.partial, partial)
        self.assertEqual("PRIVATE" * 40000, failure.partial.diagnostic)

    async def test_unconfirmed_retirement_keeps_operational_cleanup_wrapper_and_original_shortfall(self):
        ports = SessionPorts()
        self.addAsyncCleanup(ports.session.retire)
        failure = ConformerShortfall(replace(ports.batch, conformers=ports.batch.conformers[:1]))
        ports.generator.generate.side_effect = failure
        cleanup = OSError("PRIVATE_CLEANUP")
        ports.generator_host.retire.side_effect = cleanup
        try:
            with self.assertRaises(PreparationRetirementFailed) as caught:
                await ports.session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
            self.assertIs(caught.exception.operation_failure, failure)
            self.assertIs(caught.exception.failures[0][1], cleanup)
            self.assertEqual([], ports.worker.events)
        finally:
            ports.generator_host.retire.side_effect = None

    async def test_generation_failure_after_retirement_still_propagates_original_failure(self):
        ports = SessionPorts()
        entered, release = asyncio.Event(), asyncio.Event()
        failure = ConformerShortfall(replace(ports.batch, conformers=ports.batch.conformers[:1]))

        async def late(_instructions, *, require_execution):
            require_execution()
            entered.set()
            await release.wait()
            raise failure

        preparation = AsyncMock()
        preparation.prepare.side_effect = late
        ports.session = ports.make_session(preparation)
        self.addAsyncCleanup(ports.session.retire)
        task = asyncio.create_task(
            ports.session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            await ports.session.retire()
        finally:
            release.set()
        with self.assertRaises(ConformerShortfall) as caught:
            await asyncio.wait_for(task, timeout=1)
        self.assertIs(caught.exception, failure)
        self.assertEqual([], ports.worker.events)
        self.assertFalse(ports.host.confirmed)
