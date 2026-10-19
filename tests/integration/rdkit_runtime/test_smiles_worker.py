"""Run the actual native generator across the one-use private process channel.

Shortfall and unexpected failure are controlled injections, not observed natural
ETKDG failure rates. These process tests do not prove host container retirement.
"""

from tools.container_guard import require_container

require_container()

import asyncio
import hashlib
import json
import multiprocessing
import socket
import sys
import unittest
from unittest.mock import patch

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.smiles import ConformerShortfall, GenerationSettings
from nmrnet_provider.smiles_client import SmilesClient
from nmrnet_provider.smiles_worker import serve_smiles_connection
from nmrnet_provider.worker_client import WorkerReportedFailure

ALLOCATION = "native-smiles-test"
SETTINGS = GenerationSettings(seed=20261019, max_iterations=1280, timeout_seconds=1)


def serve(connection, parent, fault):
    parent.close()
    with connection:
        if fault == "shortfall":
            from rdkit.Chem import rdDistGeom  # noqa: PLC0415 -- Only the contained child may import native chemistry.

            original = rdDistGeom.EmbedMultipleConfs

            def subset(molecule, *, numConfs, params):
                identifiers = original(molecule, numConfs=numConfs, params=params)
                return list(identifiers)[::2]

            with patch.object(rdDistGeom, "EmbedMultipleConfs", side_effect=subset):
                serve_smiles_connection(connection, allocation=ALLOCATION, settings=SETTINGS, io_seconds=5)
        elif fault == "crash":
            with patch("nmrnet_provider.smiles_worker.generate_conformers", side_effect=RuntimeError("PRIVATE_WORKER_FAULT")):
                serve_smiles_connection(connection, allocation=ALLOCATION, settings=SETTINGS, io_seconds=5)
        else:
            serve_smiles_connection(connection, allocation=ALLOCATION, settings=SETTINGS, io_seconds=5)


class NativeSmilesWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, source, count, *, fault=None):
        parent, child = socket.socketpair()
        worker = multiprocessing.get_context("fork").Process(target=serve, args=(child, parent, fault))
        worker.start()
        child.close()
        try:
            reader, writer = await asyncio.open_unix_connection(sock=parent)
            client = SmilesClient(reader, writer, allocation=ALLOCATION, settings=SETTINGS, operation_seconds=5)
            try:
                return await client.generate(source, count)
            finally:
                client.close()
                await client.wait_closed()
        finally:
            parent.close()
            await asyncio.to_thread(worker.join, 1)
            if worker.is_alive():
                worker.kill()
                await asyncio.to_thread(worker.join, 1)
            try:
                self.assertEqual(worker.exitcode, 0, "Native generator did not exit cleanly after its single reply.")
                self.assertNotIn("rdkit", sys.modules, "Controller imported native chemistry.")
                self.assertNotIn("torch", sys.modules, "Conformer generation imported prediction dependencies.")
            finally:
                worker.close()

    async def test_real_requested_conformers_keep_source_identity_and_generated_hydrogens(self):
        source = b"CCl"
        batch = await self.generate(source, 3)
        metadata = json.loads(batch.provenance)
        self.assertEqual(metadata["source_sha256"], hashlib.sha256(source).hexdigest())
        self.assertEqual(metadata["requested_count"], 3)
        self.assertEqual(metadata["runtime"]["rdkit"], "2026.09.1")
        self.assertEqual([item.conformer_id for item in batch.conformers], [0, 1, 2])
        self.assertEqual(metadata["added_hydrogen_parents"], [[2, 0], [3, 0], [4, 0]])
        for item in batch.conformers:
            request = json.loads(item.request_bytes)
            self.assertEqual(request["coordinate_unit"], "angstrom")
            self.assertEqual(request["atoms"], ["C", "Cl", "H", "H", "H"])
            self.assertEqual(len(request["coordinates"]), 5)

    async def test_invalid_smiles_delivers_native_feedback_without_echoing_private_source(self):
        with self.assertRaises(InputRejected) as caught:
            await self.generate(b"C(", 1)
        self.assertIn("SMILES", str(caught.exception))
        self.assertNotIn("C(", str(caught.exception))
        self.assertTrue(caught.exception.diagnostic)

    async def test_injected_post_embedding_shortfall_keeps_actual_noncontiguous_ids(self):
        with self.assertRaises(ConformerShortfall) as caught:
            await self.generate(b"CCCC", 3, fault="shortfall")
        partial = caught.exception.partial
        self.assertEqual([item.conformer_id for item in partial.conformers], [0, 2])
        self.assertEqual(json.loads(partial.provenance)["requested_count"], 3)
        self.assertTrue(all(json.loads(item.request_bytes)["coordinates"] for item in partial.conformers))

    async def test_injected_worker_failure_is_private_and_operational(self):
        with self.assertRaises(WorkerReportedFailure) as caught:
            await self.generate(b"CCl", 1, fault="crash")
        self.assertIn("RuntimeError", caught.exception.diagnostic)
        self.assertIn("PRIVATE_WORKER_FAULT", caught.exception.diagnostic)
        self.assertNotIn("PRIVATE_WORKER_FAULT", str(caught.exception))
        self.assertNotIn("PRIVATE_WORKER_FAULT", repr(caught.exception))
