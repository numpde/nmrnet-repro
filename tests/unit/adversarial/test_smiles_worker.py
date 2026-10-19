"""Bad configuration and failed exchanges cannot restart native generation."""

from tools.container_guard import require_container

require_container()

import asyncio
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock, Mock, patch

from tests.test_provider_worker import RecordingConnection
from tests.unit.smiles_fakes import SETTINGS, SOURCE, collection

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.smiles import ConformerShortfall, GenerationSettings
from nmrnet_provider.smiles_client import SmilesClient
from nmrnet_provider.smiles_protocol import ConformerOutputLimit, conformer_reply, smiles_request
from nmrnet_provider.smiles_worker import serve_smiles_connection
from nmrnet_provider.worker_client import WorkerReportedFailure, WorkerResultRejected
from nmrnet_provider.worker_protocol import WorkerProtocolError, document, frame

ALLOCATION = "smiles-unit"


class SmilesWorkerAdversarialTests(unittest.IsolatedAsyncioTestCase):
    def client(self, reply=None):
        reader = asyncio.StreamReader()
        if reply is not None:
            reader.feed_data(frame(reply))
            reader.feed_eof()
        writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
        return SmilesClient(
            reader, writer, allocation=ALLOCATION, settings=GenerationSettings(**SETTINGS), operation_seconds=2
        ), writer

    def test_cooperative_budget_must_be_below_both_channel_deadlines_before_any_io(self):
        settings = GenerationSettings(**SETTINGS)
        for seconds in (0.5, 1):
            writer, connection = Mock(), Mock()
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                SmilesClient(Mock(), writer, allocation=ALLOCATION, settings=settings, operation_seconds=seconds)
            writer.write.assert_not_called()
            with patch("nmrnet_provider.smiles_worker.generate_conformers") as generate, self.assertRaises(ValueError):
                serve_smiles_connection(connection, allocation=ALLOCATION, settings=settings, io_seconds=seconds)
            connection.recv.assert_not_called()
            generate.assert_not_called()

    async def test_invalid_source_cancellation_and_timeout_close_without_a_retry(self):
        client, writer = self.client()
        with self.assertRaises(InputRejected):
            await client.generate(SOURCE, True)
        writer.write.assert_not_called()
        writer.close.assert_called()
        for cause in (asyncio.CancelledError(), TimeoutError("fixture deadline")):
            client, writer = self.client()
            with patch.object(client, "_exchange", new=AsyncMock(side_effect=cause)) as exchange:
                with self.assertRaises(type(cause)):
                    await client.generate(SOURCE, 2)
                with self.assertRaises(ValueError):
                    await client.generate(SOURCE, 2)
                exchange.assert_awaited_once()
            writer.close.assert_called()
            await client.wait_closed()

    async def test_another_allocations_rejection_cannot_deliver_its_private_reason(self):
        client, writer = self.client(
            {"kind": "rejected", "allocation": "foreign", "message": "PRIVATE_REASON", "diagnostic": "PRIVATE"}
        )
        with self.assertRaises(WorkerProtocolError) as caught:
            await client.generate(SOURCE, 2)
        self.assertNotIn("PRIVATE", str(caught.exception))
        writer.close.assert_called()

    async def test_native_rejection_and_unexpected_failure_reach_distinct_safe_outcomes(self):
        for cause in (InputRejected("Correct the SMILES syntax.", diagnostic="PRIVATE_NATIVE"), RuntimeError("PRIVATE_NATIVE")):
            request = smiles_request(SOURCE, 2) | {"allocation": ALLOCATION}
            connection = RecordingConnection(frame(request))
            with patch("nmrnet_provider.smiles_worker.generate_conformers", side_effect=cause):
                serve_smiles_connection(connection, allocation=ALLOCATION, settings=GenerationSettings(**SETTINGS), io_seconds=2)
            client, writer = self.client(document(connection.sent[0][4:]))
            expected_type = InputRejected if isinstance(cause, InputRejected) else WorkerReportedFailure
            with self.assertRaises(expected_type) as caught:
                await client.generate(SOURCE, 2)
            self.assertIn("PRIVATE_NATIVE", caught.exception.diagnostic)
            self.assertNotIn("PRIVATE_NATIVE", str(caught.exception))
            self.assertNotIn("PRIVATE_NATIVE", repr(caught.exception))
            writer.close.assert_called()

    async def test_output_limit_crosses_the_channel_as_failure_without_usable_coordinates(self):
        batch = collection()
        expanded = tuple(
            replace(item, request_bytes=item.request_bytes + b" " * (1024 * 1024 - len(item.request_bytes)))
            for item in batch.conformers
        )
        connection = RecordingConnection(frame(smiles_request(SOURCE, 2) | {"allocation": ALLOCATION}))
        with patch(
            "nmrnet_provider.smiles_worker.generate_conformers", return_value=replace(batch, conformers=expanded)
        ) as generate:
            serve_smiles_connection(connection, allocation=ALLOCATION, settings=GenerationSettings(**SETTINGS), io_seconds=2)
        client, writer = self.client(document(connection.sent[0][4:]))
        with self.assertRaises(ConformerOutputLimit) as caught:
            await client.generate(SOURCE, 2)
        self.assertEqual(caught.exception.actual_count, 2)
        self.assertNotIsInstance(caught.exception, InputRejected)
        self.assertNotIsInstance(caught.exception, ConformerShortfall)
        generate.assert_called_once()
        writer.close.assert_called()
        self.assertEqual(writer.write.call_count, 1)

    async def test_malformed_result_closes_and_cannot_be_retried_on_the_same_allocation(self):
        reply = conformer_reply(collection(), allocation=ALLOCATION)
        client, writer = self.client(reply | {"provenance_utf8": "[]"})
        with self.assertRaises(WorkerResultRejected):
            await client.generate(SOURCE, 2)
        with self.assertRaises(ValueError):
            await client.generate(SOURCE, 2)
        self.assertEqual(writer.write.call_count, 1)
        writer.close.assert_called()

    def test_wrong_allocation_and_wire_budget_fields_never_call_the_generator(self):
        for change in ({"allocation": "foreign"}, {"seed": 999}):
            connection = RecordingConnection(frame(smiles_request(SOURCE, 2) | {"allocation": ALLOCATION} | change))
            with patch("nmrnet_provider.smiles_worker.generate_conformers") as generate:
                if "allocation" in change:
                    with self.assertRaises(WorkerProtocolError):
                        serve_smiles_connection(
                            connection, allocation=ALLOCATION, settings=GenerationSettings(**SETTINGS), io_seconds=2
                        )
                else:
                    serve_smiles_connection(
                        connection, allocation=ALLOCATION, settings=GenerationSettings(**SETTINGS), io_seconds=2
                    )
                    self.assertEqual(document(connection.sent[0][4:])["kind"], "failure")
            generate.assert_not_called()

    def test_send_failure_cannot_trigger_a_second_frame_or_another_generation(self):
        request = smiles_request(SOURCE, 2) | {"allocation": ALLOCATION}
        connection = RecordingConnection(frame(request))
        failure = BrokenPipeError("disconnected fixture")
        connection.sendall = Mock(side_effect=failure)
        with (
            patch("nmrnet_provider.smiles_worker.generate_conformers", return_value=collection()) as generate,
            self.assertRaises(BrokenPipeError) as caught,
        ):
            serve_smiles_connection(connection, allocation=ALLOCATION, settings=GenerationSettings(**SETTINGS), io_seconds=2)
        self.assertIs(caught.exception, failure)
        connection.sendall.assert_called_once()
        generate.assert_called_once()
        self.assertIn("PRIVATE_NATIVE_DIAGNOSTIC", "\n".join(caught.exception.__notes__))

    def test_failed_error_delivery_keeps_the_native_cause_without_retry(self):
        request = smiles_request(SOURCE, 2) | {"allocation": ALLOCATION}
        for cause in (InputRejected("Correct the SMILES.", diagnostic="PRIVATE_NATIVE"), RuntimeError("PRIVATE_NATIVE")):
            connection = RecordingConnection(frame(request))
            failure = BrokenPipeError("disconnected error recipient")
            connection.sendall = Mock(side_effect=failure)
            with (
                patch("nmrnet_provider.smiles_worker.generate_conformers", side_effect=cause) as generate,
                self.assertRaises(BrokenPipeError) as caught,
            ):
                serve_smiles_connection(connection, allocation=ALLOCATION, settings=GenerationSettings(**SETTINGS), io_seconds=2)
            self.assertIs(caught.exception.__context__, cause)
            connection.sendall.assert_called_once()
            generate.assert_called_once()
