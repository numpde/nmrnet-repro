"""The one-use channel preserves generated outcomes and keeps budgets local."""

from tools.container_guard import require_container

require_container()

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from tests.test_provider_worker import RecordingConnection
from tests.unit.smiles_fakes import SETTINGS, SOURCE, collection

from nmrnet_provider.smiles import ConformerShortfall, GenerationSettings
from nmrnet_provider.smiles_client import SmilesClient
from nmrnet_provider.smiles_protocol import smiles_request
from nmrnet_provider.smiles_worker import serve_smiles_connection
from nmrnet_provider.worker_protocol import frame

ALLOCATION = "smiles-unit"


class SmilesWorkerNormativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_and_shortfall_outcomes_preserve_bytes_without_a_second_call(self):
        for identifiers in ((2, 0), (2,)):
            with self.subTest(identifiers=identifiers):
                batch = collection(identifiers)
                request = smiles_request(SOURCE, 2) | {"allocation": ALLOCATION}
                connection = RecordingConnection(frame(request))
                # silent-defaults: allow-contract -- This fixture either returns the complete batch or raises a shortfall whose exact partial the client must preserve.
                effect = ConformerShortfall(batch) if len(identifiers) < 2 else None
                with patch(
                    "nmrnet_provider.smiles_worker.generate_conformers", return_value=batch, side_effect=effect
                ) as generate:
                    serve_smiles_connection(
                        connection, allocation=ALLOCATION, settings=GenerationSettings(**SETTINGS), io_seconds=2
                    )
                generate.assert_called_once_with(SOURCE, 2, **SETTINGS)
                self.assertEqual(len(connection.sent), 1)
                reader = asyncio.StreamReader()
                reader.feed_data(connection.sent[0])
                reader.feed_eof()
                writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
                client = SmilesClient(
                    reader, writer, allocation=ALLOCATION, settings=GenerationSettings(**SETTINGS), operation_seconds=2
                )
                if effect:
                    with self.assertRaises(ConformerShortfall) as caught:
                        await client.generate(SOURCE, 2)
                    self.assertEqual(caught.exception.partial, batch)
                else:
                    self.assertEqual(await client.generate(SOURCE, 2), batch)
                writer.close.assert_called()
                await client.wait_closed()
                with self.assertRaises(ValueError):
                    await client.generate(SOURCE, 2)
                self.assertEqual(writer.write.call_count, 1)
