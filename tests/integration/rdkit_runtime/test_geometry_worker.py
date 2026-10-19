"""Exercise real MOL/SDF reading over the private socket in the admitted image.

The controller process never imports RDKit. These process tests do not replace
the separate host-owned exact-container retirement qualification.
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
from pathlib import Path

from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.geometry_worker import serve_geometry_connection
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.worker_client import WorkerReportedFailure

ALLOCATION = "native-geometry-test"


def serve(connection, parent):
    parent.close()
    with connection:
        serve_geometry_connection(connection, allocation=ALLOCATION, io_seconds=5)


class NativeGeometryWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, source, *, source_format, source_unit="angstrom", selection=None):
        parent, child = socket.socketpair()
        worker = multiprocessing.get_context("fork").Process(target=serve, args=(child, parent))
        worker.start()
        child.close()
        try:
            reader, writer = await asyncio.open_unix_connection(sock=parent)
            client = GeometryClient(reader, writer, allocation=ALLOCATION, operation_seconds=5)
            try:
                return await client.read(source, source_format=source_format, source_unit=source_unit, selection=selection)
            finally:
                client.close()
                await client.wait_closed()
        finally:
            parent.close()
            worker.join(1)
            if worker.is_alive():
                worker.kill()
                worker.join(1)
            try:
                self.assertEqual(0, worker.exitcode, "Native reader did not exit cleanly after its one reply.")
                self.assertNotIn("rdkit", sys.modules, "Controller imported the native parser.")
                self.assertNotIn("torch", sys.modules, "Structure reading imported model dependencies.")
            finally:
                worker.close()

    def setUp(self):
        self.fixtures = Path(__file__).parent / "fixtures"
        self.expected = json.loads((self.fixtures / "expected.json").read_bytes())

    def check_geometry(self, geometry, source, name):
        expected = self.expected["geometries"][name]
        request = json.loads(geometry.request_bytes)
        self.assertEqual(expected["atoms"], request["atoms"])
        self.assertEqual(expected["coordinates_angstrom"], request["coordinates"])
        self.assertEqual(tuple(range(1, len(expected["atoms"]) + 1)), geometry.source_positions)
        self.assertEqual("atom_id", geometry.source_position_kind)
        self.assertEqual(hashlib.sha256(source).hexdigest(), geometry.source_sha256)

    async def test_mol_keeps_supplied_atom_order_and_coordinates(self):
        source = (self.fixtures / "normative/atom_order_v2000.mol").read_bytes()
        (geometry,) = await self.read(source, source_format="mol")
        self.check_geometry(geometry, source, "chloromethane")
        self.assertEqual(1, geometry.source_record_number)

    async def test_mol_unit_conversion_crosses_the_wire_once(self):
        source = (self.fixtures / "normative/atom_order_v2000.mol").read_bytes()
        (geometry,) = await self.read(source, source_format="mol", source_unit="nm")
        self.assertEqual("nm", geometry.source_unit)
        self.assertEqual(10.0, geometry.scale_to_angstrom)
        self.assertEqual([0.0, 0.0, 17.8], json.loads(geometry.request_bytes)["coordinates"][1])

    async def test_sdf_all_and_selected_records_keep_original_identity_and_requested_order(self):
        source = (self.fixtures / "normative/mixed_records.sdf").read_bytes()
        for selection, numbers, names in (
            ("all", (1, 2, 3), ("chloromethane", "water", "chloromethane")),
            ((3, 1), (3, 1), ("chloromethane", "chloromethane")),
        ):
            with self.subTest(selection=selection):
                geometries = await self.read(source, source_format="sdf", selection=selection)
                self.assertEqual(numbers, tuple(geometry.source_record_number for geometry in geometries))
                for geometry, name in zip(geometries, names, strict=True):
                    self.check_geometry(geometry, source, name)

    async def test_failed_selected_sdf_suffix_rejects_without_returning_a_prefix(self):
        source = (self.fixtures / "adversarial/invalid_last.sdf").read_bytes()
        with self.assertRaisesRegex(InputRejected, "SDF record 4"):
            await self.read(source, source_format="sdf", selection="all")

    async def test_unselected_invalid_sdf_record_does_not_block_selected_records(self):
        source = (self.fixtures / "adversarial/invalid_last.sdf").read_bytes()
        geometries = await self.read(source, source_format="sdf", selection=(3, 1))
        self.assertEqual((3, 1), tuple(geometry.source_record_number for geometry in geometries))
        for geometry in geometries:
            self.check_geometry(geometry, source, "chloromethane")

    async def test_v3000_remains_a_source_rejection_through_the_wire(self):
        source = (self.fixtures / "normative/atom_order_v3000.mol").read_bytes()
        with self.assertRaisesRegex(InputRejected, "V3000"):
            await self.read(source, source_format="mol")

    async def test_real_native_parse_failure_reaches_the_protected_receiver(self):
        mol = (self.fixtures / "normative/atom_order_v2000.mol").read_bytes()
        malformed = mol.replace(b"    1.0270", b"PRIVATEBAD", 1)
        for source_format, source, selection in (
            ("mol", malformed, None),
            ("sdf", mol + b"$$$$\n" + malformed + b"$$$$\n", "all"),
        ):
            with self.subTest(source_format=source_format), self.assertRaises(WorkerReportedFailure) as failure:
                await self.read(source, source_format=source_format, selection=selection)
            self.assertIn("RDKit messages captured before the failure", failure.exception.diagnostic)
            self.assertIn("coordinates on line 5", failure.exception.diagnostic)
            self.assertIn("RuntimeError", failure.exception.diagnostic)
            self.assertNotIn("PRIVATEBAD", str(failure.exception))
            self.assertNotIn("PRIVATEBAD", repr(failure.exception))
            if source_format == "sdf":
                self.assertIn("SDF record 2", failure.exception.diagnostic)
