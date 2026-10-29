"""Generation capacity failures retain causes without admitting partial science."""

import asyncio
import hashlib
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from tests.fakes.execution import unexpected_condition
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.input_preparation import PreparationRetirementFailed
from nmrnet_provider.session import PreparationEvidenceFailed
from nmrnet_provider.smiles import ConformerShortfall
from nmrnet_provider.smiles_protocol import ConformerOutputLimit
from provider_core.analysis import AnalysisFailure


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
        outcome = await ports.session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        self.assertIs(type(outcome), AnalysisFailure)
        self.assertNotEqual(outcome.code, "invalid_input")
        self.assertNotIn("PRIVATE_", outcome.message)
        self.assertEqual(ports.worker.events, [])
        ports.generator_host.retire.assert_awaited_once()
        ports.generator.wait_closed.assert_awaited_once()
        evidence = json.loads(outcome.evidence.content)
        self.assertEqual(evidence["input_fingerprint"], ports.instructions.identity.input_fingerprint)
        generation = evidence["generation_failure"]
        self.assertEqual(generation["requested_count"], 2)
        self.assertEqual(generation["provenance_sha256"], hashlib.sha256(ports.batch.provenance).hexdigest())
        self.assertEqual(json.loads(generation["provenance_utf8"])["source_sha256"], hashlib.sha256(b"CCl").hexdigest())
        self.assertEqual(generation["diagnostic"], ports.batch.diagnostic)
        self.assertNotIn("PRIVATE_ALLOCATION", json.dumps(evidence))
        self.assertTrue({"coordinates", "request_utf8", "stereochemistry_utf8"}.isdisjoint(generation))
        originals = ports.batch.conformers[: generation["actual_count"]]
        for item, original in zip(generation["conformers"], originals, strict=True):
            self.assertEqual(
                set(item),
                {
                    "conformer_id",
                    "request_sha256",
                    "request_byte_length",
                    "stereochemistry_sha256",
                    "stereochemistry_byte_length",
                },
            )
            self.assertEqual(item["request_sha256"], hashlib.sha256(original.request_bytes).hexdigest())
            self.assertEqual(item["stereochemistry_sha256"], hashlib.sha256(original.stereochemistry).hexdigest())
            self.assertEqual(item["request_byte_length"], len(original.request_bytes))
            self.assertEqual(item["stereochemistry_byte_length"], len(original.stereochemistry))
        with self.assertRaises(ValueError):
            await ports.session.execute(object(), report_condition=unexpected_condition)
        return outcome, generation

    async def test_shortfall_preserves_actual_partial_id_and_causal_evidence_without_coordinates(self):
        ports = SessionPorts()
        failure = ConformerShortfall(replace(ports.batch, conformers=ports.batch.conformers[:1]))
        outcome, generation = await self.run_failure(ports, failure)
        self.assertEqual(outcome.code, "conformer_shortfall")
        self.assertIn("requested 2", outcome.message)
        self.assertIn("returned 1", outcome.message)
        self.assertEqual(generation["actual_count"], 1)
        self.assertEqual([item["conformer_id"] for item in generation["conformers"]], [2])

    async def test_output_limit_preserves_compact_identity_without_allocation_or_coordinates(self):
        ports = SessionPorts()
        outcome, generation = await self.run_failure(ports, output_limit(ports.batch))
        self.assertEqual(outcome.code, "conformer_output_limit")
        self.assertIn("2 MiB", outcome.message)
        self.assertEqual(generation["actual_count"], 2)
        self.assertIs(generation["coordinates_available"], False)
        self.assertEqual([item["conformer_id"] for item in generation["conformers"]], [2, 0])

    async def test_zero_returned_conformers_remain_shortfall_with_no_fabricated_payload_identities(self):
        ports = SessionPorts()
        failure = ConformerShortfall(replace(ports.batch, conformers=()))
        outcome, generation = await self.run_failure(ports, failure)
        self.assertEqual(outcome.code, "conformer_shortfall")
        self.assertEqual(generation["actual_count"], 0)
        self.assertEqual(generation["conformers"], [])

    async def test_failure_evidence_overflow_retains_original_typed_cause_and_whole_diagnostic(self):
        ports = SessionPorts()
        self.addAsyncCleanup(ports.session.retire)
        partial = replace(ports.batch, conformers=ports.batch.conformers[:1], diagnostic="PRIVATE" * 40000)
        failure = ConformerShortfall(partial)
        ports.generator.generate.side_effect = failure
        with self.assertRaises(PreparationEvidenceFailed) as caught:
            await ports.session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        self.assertIs(caught.exception.preparation_failure, failure)
        self.assertEqual(caught.exception.document["generation_failure"]["diagnostic"], partial.diagnostic)
        self.assertIsNotNone(caught.exception.__cause__)
        self.assertEqual(ports.worker.events, [])

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
            self.assertEqual(ports.worker.events, [])
        finally:
            ports.generator_host.retire.side_effect = None

    async def test_generation_failure_after_retirement_cannot_select_a_job_outcome(self):
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
        with self.assertRaisesRegex(ValueError, "retirement"):
            await asyncio.wait_for(task, timeout=1)
        self.assertEqual(ports.worker.events, [])
        self.assertFalse(ports.host.confirmed)
