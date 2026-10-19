"""Exact pasted table/XYZ bytes retain their Job and atom mapping through prediction.

Readers are real pure parsers in this contained lane. Science and host retirement
are recording ports; this does not qualify native worker or deployed API effects.
"""

import hashlib
import json
import unittest

from tests.integration.nmrnet.pasted_ports import PastedPorts


class PastedGeometryTests(unittest.IsolatedAsyncioTestCase):
    async def test_table_and_xyz_convert_once_and_keep_the_verified_selection(self):
        table = b"C 0 0 0\nH 0.125 0 0\n"
        for source_format, source, positions in (("table", table, [1, 2]), ("xyz", b"2\nauthored\n" + table, [3, 4])):
            with self.subTest(source_format=source_format):
                ports = PastedPorts(source, source_format=source_format, source_unit="nm")
                self.addAsyncCleanup(ports.session.retire)
                handle = await ports.session.prepare(ports.acknowledged, ports.instructions)
                self.assertEqual(ports.worker.events, [])
                ports.reader_host.retire.assert_awaited_once()
                ports.reader.wait_closed.assert_awaited_once()
                identity = json.loads(handle.identity.content)["geometries"][0]
                outcome = await ports.session.execute(handle)
                (geometry,) = json.loads(outcome.content)["geometries"]
                expected = {
                    "sha256": hashlib.sha256(source).hexdigest(),
                    "record_number": 1,
                    "position_kind": "line_number",
                    "positions": positions,
                    "job_ref": ports.instructions.job_ref,
                    "input_fingerprint": ports.instructions.identity.input_fingerprint,
                    "start_byte": ports.start_byte,
                    "end_byte": ports.end_byte,
                    "byte_length": len(source),
                    "format": source_format,
                    "positions_relative_to": "selected_byte_span",
                }
                self.assertEqual(geometry["source"], expected)
                self.assertEqual(identity["source"], expected)
                self.assertEqual(geometry["prepared_request"]["coordinates"], [[0, 0, 0], [1.25, 0, 0]])
                self.assertEqual(geometry["prepared_request"]["coordinate_unit"], "angstrom")
                self.assertEqual(geometry["preparation"]["scale_to_angstrom"], 10)
                self.assertEqual(geometry["preparation"]["source_unit"], "nm")
                self.assertEqual(geometry["coverage"]["hydrogen_completeness"], "not_established_from_coordinates")
                self.assertEqual([row["atom_index"] for row in geometry["predictions"]], [0, 1])
                self.assertEqual(ports.reader.read.await_args.args, (source,))
                options = ports.reader.read.await_args.kwargs
                self.assertEqual(options["source_format"], source_format)
                self.assertEqual(options["source_unit"], "nm")
                if source_format == "xyz":
                    self.assertEqual(options["selection"], "all")
                else:
                    self.assertIsNone(options["selection"])
                ports.uploads.acquire.assert_not_awaited()
                ports.allocate_generator.assert_not_awaited()
