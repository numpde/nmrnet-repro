"""Diagnostic capacity failures preserve evidence without dispatch or retries."""

import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.session import PreparationEvidenceFailed, ResultAssemblyFailed
from provider_core.attempt_record import MAX_EVIDENCE_BYTES


class SmilesSessionRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_preparation_diagnostic_overflow_keeps_full_evidence_before_worker_dispatch(self):
        ports = SessionPorts()
        self.addAsyncCleanup(ports.session.retire)
        diagnostic = "PRIVATE_NATIVE_DIAGNOSTIC" * 14000
        ports.batch = replace(ports.batch, diagnostic=diagnostic)
        ports.generator.generate.return_value = ports.batch
        with self.assertRaises(PreparationEvidenceFailed) as caught:
            await ports.session.prepare(ports.acknowledged, ports.instructions)
        self.assertEqual(caught.exception.document["preparation_diagnostics"][0]["diagnostic"], diagnostic)
        self.assertIsNotNone(caught.exception.__cause__)
        self.assertNotIn("coordinates", json.dumps(caught.exception.document))
        self.assertEqual(ports.worker.events, [])
        ports.generator_host.retire.assert_awaited_once()

    async def test_combined_terminal_overflow_keeps_exact_worker_and_prepared_evidence(self):
        ports = SessionPorts()
        self.addAsyncCleanup(ports.session.retire)
        ports.batch = replace(ports.batch, diagnostic="PRIVATE_GENERATOR_" * 8000)
        ports.generator.generate.return_value = ports.batch
        handle = await ports.session.prepare(ports.acknowledged, ports.instructions)
        outcomes = tuple(ports.worker.outcome(item.request_bytes)[0] for item in ports.batch.conformers)
        outcomes[0]["private_warnings"] = [
            {
                "category": "builtins.UserWarning",
                "head": "H",
                "versions": None,
                "message": "PRIVATE_MODEL_" * 11000,
                "filename": "/private",
                "lineno": 1,
            }
        ]
        self.assertLess(len(json.dumps(outcomes)), MAX_EVIDENCE_BYTES)
        with patch.object(ports.worker, "execute", new=AsyncMock(return_value=outcomes)) as execute:
            with self.assertRaises(ResultAssemblyFailed) as caught:
                await ports.session.execute(handle)
            self.assertIs(caught.exception.worker_result, outcomes)
            self.assertIs(caught.exception.prepared_identity, handle.identity)
            self.assertIn(b"PRIVATE_GENERATOR_", caught.exception.prepared_identity.content)
            self.assertIsNotNone(caught.exception.__cause__)
            execute.assert_awaited_once()
        with self.assertRaises(ValueError):
            await ports.session.execute(handle)
