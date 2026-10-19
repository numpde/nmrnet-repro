"""Read actual sources across a process boundary without model or native imports."""

from tools.container_guard import require_container

require_container()

import asyncio
import base64
import hashlib
import json
import multiprocessing
import socket
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from tests.integration.provider_worker import retirement_probe
from tests.test_provider_worker import RecordingConnection

from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.geometry_protocol import prepared_reply, read_source, source_request
from nmrnet_provider.geometry_worker import serve_geometry_connection
from nmrnet_provider.input_geometry import InputRejected, prepare_table
from nmrnet_provider.worker_client import WorkerReportedFailure
from nmrnet_provider.worker_protocol import MAX_FRAME_BYTES, WorkerProtocolError, document, frame

FIXTURES = Path(__file__).parent / "fixtures/preparation"
ALLOCATION = "geometry-test"

# The host launcher mounts this shared helper as /workspace/probe.py. Import
# that same helper here without mounting or importing an engine client.
with patch.dict(sys.modules, {"probe": retirement_probe}):
    from tests.integration.provider_worker import geometry_probe


class NativeStallQualificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.case = geometry_probe.GeometryInput(b"supplied MOL bytes", "mol", None)

    async def test_native_timeout_cannot_wait_for_a_late_stall_marker(self):
        client = Mock(read=AsyncMock(side_effect=TimeoutError))
        with (
            patch.object(Path, "read_bytes", side_effect=FileNotFoundError),
            patch.object(geometry_probe, "wait_for", return_value=(ALLOCATION + "\n").encode()) as waiter,
            self.assertRaisesRegex(RuntimeError, "stall"),
        ):
            await geometry_probe.read_case(client, self.case, ALLOCATION, "geometry-native-timeout")
        waiter.assert_not_called()

    async def test_native_timeout_requires_an_existing_allocation_matched_marker(self):
        for marker, accepted in (((ALLOCATION + "\n").encode(), True), (b"other-allocation\n", False)):
            with self.subTest(accepted=accepted):
                client = Mock(read=AsyncMock(side_effect=TimeoutError))
                with (
                    patch.object(Path, "read_bytes", return_value=marker),
                    patch.object(geometry_probe, "wait_for", side_effect=AssertionError("Late marker wait is forbidden.")),
                ):
                    if accepted:
                        self.assertEqual(
                            (), await geometry_probe.read_case(client, self.case, ALLOCATION, "geometry-native-timeout")
                        )
                    else:
                        with self.assertRaisesRegex(RuntimeError, "stall"):
                            await geometry_probe.read_case(client, self.case, ALLOCATION, "geometry-native-timeout")


class NativeGeometryProtocolTests(unittest.TestCase):
    """Check dispatch and receiver admission with injected reader outputs, without RDKit."""

    def geometry(self):
        return replace(
            prepare_table(b"C 0 0 0\nH 0.109 0 0\n", source_unit="nm"),
            source_position_kind="atom_id",
            diagnostic="PRIVATE_NATIVE_WARNING",
        )

    def test_mol_dispatch_keeps_atom_rows_units_and_original_source_identity(self):
        source = (FIXTURES / "normative/atom_order_v2000.mol").read_bytes()
        request = source_request(source, source_format="mol", source_unit="nm", selection=None)
        with patch("nmrnet_provider.geometry_protocol.prepare_mol", return_value=self.geometry()) as reader:
            records = read_source(request)
        reader.assert_called_once_with(source, source_unit="nm")
        (actual,) = prepared_reply(
            {"kind": "geometries", "allocation": ALLOCATION, "records": json.loads(json.dumps(records))},
            source,
            source_format="mol",
            source_unit="nm",
            selection=None,
        )
        self.assertEqual(self.geometry().request_bytes, actual.request_bytes)
        self.assertEqual((1, 2), actual.source_positions)
        self.assertEqual("atom_id", actual.source_position_kind)
        self.assertEqual(hashlib.sha256(source).hexdigest(), actual.source_sha256)
        self.assertEqual(10.0, actual.scale_to_angstrom)
        self.assertEqual(1, actual.source_record_number)
        self.assertEqual("PRIVATE_NATIVE_WARNING", actual.diagnostic)
        self.assertNotIn("PRIVATE_NATIVE_WARNING", repr(actual))

    def test_sdf_dispatch_preserves_selected_order_and_whole_source_identity(self):
        source = (FIXTURES / "normative/mixed_records.sdf").read_bytes()
        geometry = self.geometry()
        for selection, numbers in (("all", (1, 2, 3)), ((3, 1), (3, 1))):
            with self.subTest(selection=selection):
                request = source_request(source, source_format="sdf", source_unit="nm", selection=selection)
                geometries = tuple(replace(geometry, source_record_number=number) for number in numbers)
                with patch("nmrnet_provider.geometry_protocol.prepare_sdf", return_value=geometries) as reader:
                    records = read_source(request)
                reader.assert_called_once_with(source, source_unit="nm", selection=selection)
                actual = prepared_reply(
                    {"kind": "geometries", "allocation": ALLOCATION, "records": json.loads(json.dumps(records))},
                    source,
                    source_format="sdf",
                    source_unit="nm",
                    selection=selection,
                )
                self.assertEqual(numbers, tuple(item.source_record_number for item in actual))
                self.assertTrue(all(item.source_sha256 == hashlib.sha256(source).hexdigest() for item in actual))
                self.assertTrue(all(item.source_positions == (1, 2) for item in actual))

    def test_single_mol_and_selected_sdf_require_explicit_selection_shape(self):
        cases = (("mol", "all"), ("mol", (1,)), ("sdf", None), ("sdf", (True,)), ("sdf", (1, 1)), ("sdf", (129,)))
        for source_format, selection in cases:
            with self.subTest(source_format=source_format, selection=selection), self.assertRaises(InputRejected):
                source_request(b"source", source_format=source_format, source_unit="angstrom", selection=selection)

    def test_native_atom_ids_must_match_original_one_based_rows(self):
        geometry = self.geometry()
        record = {
            "request_base64": base64.b64encode(geometry.request_bytes).decode("ascii"),
            "source_positions": [1, 2],
            "source_position_kind": "atom_id",
            "source_record_number": 1,
            "diagnostic": None,
        }
        for source_format in ("mol", "sdf"):
            selection = None if source_format == "mol" else (1,)
            valid_reply = {"kind": "geometries", "allocation": ALLOCATION, "records": [record]}
            prepared_reply(valid_reply, b"source", source_format=source_format, source_unit="angstrom", selection=selection)
            for changed in (
                {"source_positions": [0, 1]},
                {"source_positions": [2, 1]},
                {"source_positions": [1, 3]},
                {"source_positions": [1, 1]},
                {"source_positions": [True, 2]},
                {"source_position_kind": "line_number"},
                {"source_record_number": 2},
            ):
                with self.subTest(source_format=source_format, changed=changed), self.assertRaises(WorkerProtocolError):
                    prepared_reply(
                        valid_reply | {"records": [record | changed]},
                        b"source",
                        source_format=source_format,
                        source_unit="angstrom",
                        selection=selection,
                    )


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
    async def test_private_diagnostic_round_trips_without_entering_geometry_repr(self):
        source = b"C 0 0 0\n"
        geometry = prepare_table(source, source_unit="angstrom")
        for diagnostic in (None, "", "PRIVATE_READER_WARNING \u96ea\n[RDKit diagnostic truncated]\n"):
            with (
                self.subTest(diagnostic_kind=type(diagnostic).__name__),
                patch("nmrnet_provider.geometry_protocol.prepare_table", return_value=replace(geometry, diagnostic=diagnostic)),
            ):
                client = await self.client(fault=False)
                (actual,) = await client.read(source, source_format="table", source_unit="angstrom", selection=None)
                self.assertEqual(diagnostic, actual.diagnostic)
                self.assertNotIn("PRIVATE_READER_WARNING", repr(actual))

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
    async def test_expected_rejection_retains_its_private_reason_separately(self):
        for diagnostic in (None, "", "PRIVATE_REJECTION_REASON \u96ea\n[RDKit diagnostic truncated]\n"):
            cause = InputRejected("The selected source needs correction.", diagnostic=diagnostic)
            with (
                self.subTest(diagnostic_kind=type(diagnostic).__name__),
                patch("nmrnet_provider.geometry_worker.read_source", side_effect=cause),
            ):
                client = await self.client(fault=False)
                with self.assertRaises(InputRejected) as failure:
                    await client.read(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)
                self.assertEqual(diagnostic, failure.exception.diagnostic)
                self.assertNotIn("PRIVATE_REJECTION_REASON", str(failure.exception))
                self.assertNotIn("PRIVATE_REJECTION_REASON", repr(failure.exception))

    async def test_inconclusive_native_failure_retains_its_private_note(self):
        cause = RuntimeError("RDKit returned no geometry; the cause is inconclusive.")
        cause.add_note("PRIVATE_NATIVE_NOTE")
        with patch("nmrnet_provider.geometry_worker.read_source", side_effect=cause):
            client = await self.client(fault=False)
            with self.assertRaises(WorkerReportedFailure) as failure:
                await client.read(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)
        self.assertIn("RuntimeError", failure.exception.diagnostic)
        self.assertIn("PRIVATE_NATIVE_NOTE", failure.exception.diagnostic)
        self.assertNotIn("PRIVATE_NATIVE_NOTE", str(failure.exception))

    async def test_table_atom_limit_precedes_parsing_an_excess_row(self):
        client = await self.client(fault=False)
        source = b"C 0 0 0\n" * 128 + b"C PRIVATE_EXCESS_ROW 0 0\n"
        with self.assertRaisesRegex(InputRejected, "128-atom limit") as failure:
            await client.read(source, source_format="table", source_unit="angstrom", selection=None)
        self.assertNotIn("PRIVATE_EXCESS_ROW", str(failure.exception))

    async def test_oversized_complete_output_is_a_rejection_without_partial_records(self):
        request = source_request(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)
        records = read_source(request)
        records[0]["request_base64"] = "x" * MAX_FRAME_BYTES
        with patch("nmrnet_provider.geometry_worker.read_source", return_value=records):
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

    async def test_nontext_private_diagnostic_is_a_protocol_failure(self):
        source = b'{"atoms":["C"],"coordinates":[[0,0,0]],"coordinate_unit":"angstrom"}'
        record = {
            "request_base64": base64.b64encode(source).decode(),
            "source_positions": [0],
            "source_position_kind": "atom_index",
            "source_record_number": 1,
            "diagnostic": ["PRIVATE_MALFORMED_DIAGNOSTIC"],
        }
        replies = (
            {"kind": "geometries", "allocation": ALLOCATION, "records": [record]},
            {"kind": "rejected", "allocation": ALLOCATION, "message": "Correct the source.", "diagnostic": {}},
        )
        for reply in replies:
            with self.subTest(kind=reply["kind"]):
                client, writer = self.client(reply)
                with self.assertRaises(WorkerProtocolError) as failure:
                    await client.read(source, source_format="json", source_unit="angstrom", selection=None)
                self.assertNotIn("PRIVATE_MALFORMED_DIAGNOSTIC", str(failure.exception))
                writer.close.assert_called()
                await client.wait_closed()

    async def test_wrong_allocation_cannot_deliver_private_rejection_evidence(self):
        client, writer = self.client(
            {
                "kind": "rejected",
                "allocation": "another-worker",
                "message": "Correct the source.",
                "diagnostic": "PRIVATE_OTHER_WORKER",
            }
        )
        with self.assertRaises(WorkerProtocolError) as failure:
            await client.read(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)
        self.assertNotIn("PRIVATE_OTHER_WORKER", str(failure.exception))
        writer.close.assert_called()
        await client.wait_closed()

    async def test_changed_direct_json_or_false_source_mapping_is_not_accepted(self):
        source = b'{"atoms":["C"],"coordinates":[[0,0,0]],"coordinate_unit":"angstrom"}'
        record = {
            "request_base64": base64.b64encode(source).decode(),
            "source_positions": [0],
            "source_position_kind": "atom_index",
            "source_record_number": 1,
            "diagnostic": None,
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


class GeometryFailureFramingTests(unittest.TestCase):
    def test_success_transport_failure_retains_reader_reason_without_retry(self):
        request = source_request(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)
        records = read_source(request)
        records[0]["diagnostic"] = "PRIVATE_READER_REASON"
        connection = RecordingConnection(frame(request | {"allocation": ALLOCATION}))
        cause = BrokenPipeError("Worker reply channel closed.")
        with (
            patch("nmrnet_provider.geometry_worker.read_source", return_value=records),
            patch.object(connection, "sendall", side_effect=cause) as sender,
            self.assertRaises(BrokenPipeError) as failure,
        ):
            serve_geometry_connection(connection, allocation=ALLOCATION, io_seconds=1)
        self.assertTrue(failure.exception is cause, "Transport failure was replaced.")
        self.assertTrue(any("PRIVATE_READER_REASON" in note for note in getattr(cause, "__notes__", ())))
        self.assertNotIn("PRIVATE", str(cause))
        sender.assert_called_once()

    def test_diagnostics_count_toward_the_complete_batch_frame_limit(self):
        source = b"1\nfirst\nC 0 0 0\n1\nsecond\nH 1 0 0\n"
        request = source_request(source, source_format="xyz", source_unit="angstrom", selection="all")
        records = read_source(request)
        records[0]["diagnostic"] = ""
        reply = {"kind": "geometries", "allocation": ALLOCATION, "records": records}
        available = MAX_FRAME_BYTES - (len(frame(reply)) - 4)
        records[0]["diagnostic"] = "x" * available
        for excess in (0, 1):
            with self.subTest(excess=excess):
                connection = RecordingConnection(frame(request | {"allocation": ALLOCATION}))
                with patch("nmrnet_provider.geometry_worker.read_source", return_value=records):
                    serve_geometry_connection(connection, allocation=ALLOCATION, io_seconds=1)
                self.assertEqual(1, len(connection.sent))
                actual = document(connection.sent[0][4:])
                if excess == 0:
                    self.assertEqual(MAX_FRAME_BYTES + 4, len(connection.sent[0]))
                    self.assertEqual("geometries", actual["kind"])
                    self.assertEqual(2, len(actual["records"]))
                    records[0]["diagnostic"] += "x"
                else:
                    self.assertEqual("rejected", actual["kind"])
                    self.assertNotIn("records", actual)
                    self.assertIn("complete output", actual["message"])
                    self.assertIn("Record 1:", actual["diagnostic"])
                    self.assertIn("[Batch reader diagnostics truncated]", actual["diagnostic"])
                    self.assertLess(len(actual["diagnostic"]), 8300)

    def test_output_size_rejection_keeps_ordered_private_reader_reasons(self):
        request = source_request(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)
        records = [
            {"source_record_number": 3, "diagnostic": "PRIVATE_FIRST", "request_base64": "x" * MAX_FRAME_BYTES},
            {"source_record_number": 1, "diagnostic": "PRIVATE_SECOND"},
        ]
        connection = RecordingConnection(frame(request | {"allocation": ALLOCATION}))
        with patch("nmrnet_provider.geometry_worker.read_source", return_value=records):
            serve_geometry_connection(connection, allocation=ALLOCATION, io_seconds=1)
        reply = document(connection.sent[0][4:])
        self.assertEqual("rejected", reply["kind"])
        self.assertEqual("Record 3:\nPRIVATE_FIRST\nRecord 1:\nPRIVATE_SECOND", reply["diagnostic"])
        self.assertNotIn("PRIVATE", reply["message"])

    def test_unsendable_rejection_keeps_its_safe_reason_and_private_diagnostic(self):
        request = source_request(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)
        connection = RecordingConnection(frame(request | {"allocation": ALLOCATION}))
        cause = InputRejected("Correct the selected source.", diagnostic="PRIVATE_REJECTION " + "x" * MAX_FRAME_BYTES)
        with (
            patch("nmrnet_provider.geometry_worker.read_source", side_effect=cause),
            self.assertRaises(WorkerProtocolError) as failure,
        ):
            serve_geometry_connection(connection, allocation=ALLOCATION, io_seconds=1)
        self.assertTrue(failure.exception.__context__ is cause, "Original rejection context was lost.")
        self.assertIn("PRIVATE_REJECTION", cause.__notes__[0][:100])
        self.assertNotIn("PRIVATE_REJECTION", str(cause))
        self.assertEqual([], connection.sent)

    def test_oversized_failure_retains_original_exception_context(self):
        request = source_request(b"C 0 0 0", source_format="table", source_unit="angstrom", selection=None)
        connection = RecordingConnection(frame(request | {"allocation": ALLOCATION}))
        cause = RuntimeError("PRIVATE_NATIVE_FAILURE")
        cause.add_note("x" * (2 * 1024 * 1024))
        with (
            patch("nmrnet_provider.geometry_worker.read_source", side_effect=cause),
            self.assertRaises(WorkerProtocolError) as failure,
        ):
            serve_geometry_connection(connection, allocation=ALLOCATION, io_seconds=1)
        self.assertTrue(failure.exception.__context__ is cause, "Original failure context was lost.")
        self.assertEqual([], connection.sent)


if __name__ == "__main__":
    unittest.main()
