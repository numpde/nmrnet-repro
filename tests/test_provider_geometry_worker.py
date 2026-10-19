"""Read actual sources across a process boundary without model or native imports."""

from tools.container_guard import require_container

require_container()

import asyncio
import base64
import hashlib
import json
import multiprocessing
import socket
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.geometry_worker import serve_geometry_connection
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.worker_client import WorkerReportedFailure
from nmrnet_provider.worker_protocol import WorkerProtocolError, frame

FIXTURES = Path(__file__).parent / "fixtures/preparation"
ALLOCATION = "geometry-test"


def run_reader(connection, parent, fault):
    parent.close()
    with connection:
        if fault:
            with patch("nmrnet_provider.geometry_worker.read_source", side_effect=OSError("PRIVATE_PARSER_CAUSE")):
                serve_geometry_connection(connection, allocation=ALLOCATION, io_seconds=1)
        else:
            serve_geometry_connection(connection, allocation=ALLOCATION, io_seconds=1)


class GeometryProcessTests(unittest.IsolatedAsyncioTestCase):
    async def client(self, *, fault):
        parent, child = socket.socketpair()
        worker = multiprocessing.get_context("fork").Process(target=run_reader, args=(child, parent, fault))
        worker.start()
        child.close()

        def finish():
            worker.join(1)
            if worker.is_alive():
                worker.kill()
                worker.join(1)
            worker.close()

        self.addCleanup(finish)
        reader, writer = await asyncio.open_unix_connection(sock=parent)
        client = GeometryClient(reader, writer, allocation=ALLOCATION, operation_seconds=1)
        self.addAsyncCleanup(client.wait_closed)
        self.addCleanup(client.close)
        return client


class GeometryWorkerNormativeTests(GeometryProcessTests):
    async def test_json_keeps_exact_bytes_and_original_atom_indices(self):
        source = b' {"atoms":["H","C"],"coordinates":[[1.09,0,0],[0,0,0]],"coordinate_unit":"angstrom"}\n'
        client = await self.client(fault=False)
        (geometry,) = await client.read(source, source_format="json", source_unit="angstrom", selection=None)
        self.assertEqual(source, geometry.request_bytes)
        self.assertEqual(hashlib.sha256(source).hexdigest(), geometry.source_sha256)
        self.assertEqual((0, 1), geometry.source_positions)
        self.assertEqual("atom_index", geometry.source_position_kind)
        self.assertEqual(1, geometry.source_record_number)

    async def test_table_converts_once_and_retains_original_lines(self):
        source = b"\nH 0.109 0 0\nC 0 0 0\n"
        client = await self.client(fault=False)
        (geometry,) = await client.read(source, source_format="table", source_unit="nm", selection=None)
        self.assertEqual((2, 3), geometry.source_positions)
        self.assertEqual("nm", geometry.source_unit)
        self.assertEqual(10.0, geometry.scale_to_angstrom)
        self.assertEqual("NIST SP 811 Appendix B.8", geometry.conversion_reference)
        self.assertAlmostEqual(1.09, json.loads(geometry.request_bytes)["coordinates"][0][0], places=12)

    async def test_selected_xyz_records_keep_order_and_source_correspondence(self):
        source = (FIXTURES / "normative/mixed_records.xyz").read_bytes()
        expected = json.loads((FIXTURES / "expected.json").read_bytes())["geometries"]["chloromethane"]
        client = await self.client(fault=False)
        result = await client.read(source, source_format="xyz", source_unit="angstrom", selection=(3, 1))
        self.assertEqual((3, 1), tuple(geometry.source_record_number for geometry in result))
        for geometry, positions in zip(result, ((15, 16, 17, 18, 19), (3, 4, 5, 6, 7)), strict=True):
            self.assertEqual(positions, geometry.source_positions)
            self.assertEqual(expected["atoms"], json.loads(geometry.request_bytes)["atoms"])
            self.assertEqual(expected["coordinates_angstrom"], json.loads(geometry.request_bytes)["coordinates"])

    async def test_all_xyz_records_preserve_different_molecules(self):
        source = (FIXTURES / "normative/mixed_records.xyz").read_bytes()
        expected = json.loads((FIXTURES / "expected.json").read_bytes())["geometries"]
        client = await self.client(fault=False)
        result = await client.read(source, source_format="xyz", source_unit="angstrom", selection="all")
        self.assertEqual((1, 2, 3), tuple(geometry.source_record_number for geometry in result))
        for geometry, name in zip(result, ("chloromethane", "water", "chloromethane"), strict=True):
            self.assertEqual(expected[name]["atoms"], json.loads(geometry.request_bytes)["atoms"])


class GeometryWorkerAdversarialTests(GeometryProcessTests):
    async def test_table_atom_limit_precedes_parsing_an_excess_row(self):
        client = await self.client(fault=False)
        source = b"C 0 0 0\n" * 128 + b"C PRIVATE_EXCESS_ROW 0 0\n"
        with self.assertRaisesRegex(InputRejected, "128-atom limit") as failure:
            await client.read(source, source_format="table", source_unit="angstrom", selection=None)
        self.assertNotIn("PRIVATE_EXCESS_ROW", str(failure.exception))

    async def test_oversized_complete_output_is_a_rejection_without_partial_records(self):
        with patch("nmrnet_provider.geometry_worker.read_source", return_value=[{"oversized": "x" * (2 * 1024 * 1024)}]):
            client = await self.client(fault=False)
            with self.assertRaisesRegex(InputRejected, "complete output exceeds 2 MiB"):
                await client.read(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)

    async def test_malformed_worker_output_is_operational_failure_not_input_feedback(self):
        with patch("nmrnet_provider.geometry_worker.read_source", return_value=[{"invalid": float("nan")}]):
            client = await self.client(fault=False)
            with self.assertRaises(WorkerReportedFailure) as failure:
                await client.read(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)
            self.assertIn("ValueError", failure.exception.diagnostic)

    async def test_selected_bad_later_record_rejects_without_partial_result(self):
        client = await self.client(fault=False)
        source = (FIXTURES / "adversarial/invalid_last.xyz").read_bytes()
        with self.assertRaisesRegex(InputRejected, "record 4") as failure:
            await client.read(source, source_format="xyz", source_unit="angstrom", selection="all")
        self.assertNotIn("PRIVATE_COORDINATE_CANARY", str(failure.exception))

    async def test_parser_crash_keeps_private_cause_separate_from_public_text(self):
        client = await self.client(fault=True)
        with self.assertRaises(WorkerReportedFailure) as failure:
            await client.read(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)
        self.assertNotIn("PRIVATE_PARSER_CAUSE", str(failure.exception))
        self.assertIn("PRIVATE_PARSER_CAUSE", failure.exception.diagnostic)

    async def test_malformed_json_does_not_fall_back_to_another_reader(self):
        client = await self.client(fault=False)
        with self.assertRaises(InputRejected):
            await client.read(b"C 0 0 0", source_format="json", source_unit="angstrom", selection=None)


class GeometryReplyAdversarialTests(unittest.IsolatedAsyncioTestCase):
    def client(self, reply):
        reader = asyncio.StreamReader()
        reader.feed_data(frame(reply))
        reader.feed_eof()
        writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
        return GeometryClient(reader, writer, allocation=ALLOCATION, operation_seconds=0.02), writer

    async def test_changed_direct_json_or_false_source_mapping_is_not_accepted(self):
        source = b'{"atoms":["C"],"coordinates":[[0,0,0]],"coordinate_unit":"angstrom"}'
        record = {
            "request_base64": base64.b64encode(source).decode(),
            "source_positions": [0],
            "source_position_kind": "atom_index",
            "source_record_number": 1,
        }
        for changed in (
            {"request_base64": base64.b64encode(source + b" ").decode()},
            {"source_positions": [1]},
            {"source_record_number": True},
            {"source_position_kind": "unknown"},
        ):
            with self.subTest(changed=changed):
                client, writer = self.client({"kind": "geometries", "allocation": ALLOCATION, "records": [record | changed]})
                with self.assertRaises(WorkerProtocolError):
                    await client.read(source, source_format="json", source_unit="angstrom", selection=None)
                writer.close.assert_called()
                await client.wait_closed()

    async def test_oversized_source_and_second_read_do_not_dispatch(self):
        client, writer = self.client({"kind": "unused", "allocation": ALLOCATION})
        with self.assertRaisesRegex(InputRejected, "1 MiB"):
            await client.read(b"x" * (1024 * 1024 + 1), source_format="xyz", source_unit="angstrom", selection="all")
        with self.assertRaisesRegex(ValueError, "one source"):
            await client.read(b"1\n\nC 0 0 0\n", source_format="xyz", source_unit="angstrom", selection="all")
        writer.write.assert_not_called()
        await client.wait_closed()

    async def test_timeout_closes_channel_without_claiming_worker_retirement(self):
        reader = asyncio.StreamReader()
        writer = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
        client = GeometryClient(reader, writer, allocation=ALLOCATION, operation_seconds=0.01)
        with self.assertRaises(TimeoutError):
            await client.read(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)
        writer.close.assert_called()
        await client.wait_closed()


if __name__ == "__main__":
    unittest.main()
