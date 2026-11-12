"""Authored source records and scripted selections for format correction tests.

The authored expected.json owns coordinates and source correspondence. Scripted
tool choices exercise reading, not language-model understanding or NMR accuracy.
"""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.interpreter_ports import tool_reply
from tests.integration.nmrnet.reference_fixtures import NM_TABLE, nm_conversion_oracle

FORMAT_CASES = (
    "json",
    "table",
    "xyz",
    "mol",
    "sdf-all",
    "sdf-correction",
    "pdb-models",
    "pdb-alternate",
    "mol2",
    "sdf-carbon-only",
)


from nmrnet_provider.prepared_input import MAX_SHIFT_SIGNIFICANT_DIGITS

class FormatFixture:
    """Keep independent molecular facts beside their selected source records."""

    input_kind = "upload"
    fixture_root = Path("/input/preparation")

    def load_case(self):
        """Load independent molecular expectations and scripted preparation choices."""
        oracle = (self.fixture_root / "expected.json").read_bytes()
        corpus = json.loads(oracle)
        self.oracle_sha256 = hashlib.sha256(oracle).hexdigest()
        self.expected_records = (1,)
        self.relative_source = None
        self.reader_options = {
            "source_format": self.format_case,
            "source_unit": "angstrom",
            "selection": None,
            "alternate_location": None,
        }
        if self.format_case == "json":
            self.relative_source = "normative/atom_order.json"
            self.expected = [corpus["geometries"]["chloromethane"]]
            self.expected_positions = (tuple(range(5)),)
        elif self.format_case == "table":
            self.source = NM_TABLE
            self.reader_options["source_unit"] = "nm"
            self.expected = [corpus["geometries"]["chloromethane"]]
            self.expected_positions = ((1, 2, 3, 4, 5),)
            exact = json.loads(oracle, parse_float=Decimal)["geometries"]["chloromethane"]
            rows = [row.split() for row in self.source.decode().splitlines()]
            self.coordinate_oracle = [
                [nm_conversion_oracle(value, intended) for value, intended in zip(row[1:], target, strict=True)]
                for row, target in zip(rows, exact["coordinates_angstrom"], strict=True)
            ]
        elif self.format_case == "xyz":
            self.relative_source = "normative/mixed_records.xyz"
            self.expected_records = (2, 1)
            self.expected = [corpus["geometries"][name] for name in ("water", "chloromethane")]
            self.expected_positions = ((10, 11, 12), (3, 4, 5, 6, 7))
            self.reader_options["selection"] = self.expected_records
        elif self.format_case == "pdb-alternate":
            self.relative_source = "normative/alternate_locations.pdb"
            expected = corpus["native_files"][self.relative_source]["selection_B"]
            self.expected = [{key: expected[key] for key in ("atoms", "coordinates_angstrom")}]
            self.expected_positions = (tuple(expected["source_atom_ids"]),)
            self.reader_options.update(source_format="pdb", selection=(1,), alternate_location="B")
        else:
            source_format, relative_source, records, selection = {
                "mol": ("mol", "normative/atom_order_v2000.mol", (1,), None),
                "sdf-all": ("sdf", "normative/mixed_records.sdf", (1, 2, 3), "all"),
                "sdf-carbon-only": ("sdf", "normative/butane_conformers.sdf", (1, 2), "all"),
                "sdf-correction": ("sdf", "adversarial/invalid_last.sdf", (3, 1), (3, 1)),
                "pdb-models": ("pdb", "normative/models.pdb", (2, 1), (2, 1)),
                "mol2": ("mol2", "normative/mixed_records.mol2", (2, 1), (2, 1)),
            }[self.format_case]
            self.relative_source = relative_source
            # silent-defaults: allow-contract -- The malformed SDF adds an invalid trailing record; its selected valid records retain the independent mixed-record oracle.
            native = corpus["native_files"][
                "normative/mixed_records.sdf" if self.format_case == "sdf-correction" else relative_source
            ]
            self.expected_records = records
            self.expected = [corpus["geometries"][native["geometry_names"][record - 1]] for record in records]
            # repo-check: allow-tuple-generator -- The fixture preserves tuple-valued source positions for each ordered geometry; proof=nmrnet_provider/input_geometry.py::PreparedGeometry.
            self.expected_positions = tuple(tuple(native["source_atom_ids"][record - 1]) for record in records)
            self.reader_options.update(source_format=source_format, selection=selection)
        self.preparation_options = [self.reader_options]
        self.correction_feedback = None
        correction = {
            "sdf-correction": ("record 4", {"selection": "all"}),
            "pdb-alternate": ("alternate", {"alternate_location": None}),
        }.get(self.format_case)
        if correction is not None:
            self.correction_feedback, initial_options = correction
            self.preparation_options.insert(0, self.reader_options | initial_options)
        self.expected_preparation_calls = len(self.preparation_options)
        self.expected_model_calls = 1 + self.expected_preparation_calls
        if self.relative_source is not None:
            self.source = (self.fixture_root / self.relative_source).read_bytes()
        self.fixture_sha256 = hashlib.sha256(self.source).hexdigest()
        options = self.reader_options
        self.instructions = (
            f"Predict the attached {self.format_case} fixture as {options['source_format']}, source unit {options['source_unit']}; "
            f"select {options['selection']!r} in that order, alternate location {options['alternate_location']!r}. "
            f"Keep supplied atoms and coordinates. Return shifts with {MAX_SHIFT_SIGNIFICANT_DIGITS} significant digits."
        ).encode()

    async def model_reply(self, request, raw):
        """Script source selection, observing reader retirement before correction."""
        self.assertIn(b"authorization: bearer private_model_key\r\n", request.lower())
        self.assertNotIn(b"signature:", request.lower())
        self.model_calls += 1
        messages = json.loads(raw)["messages"]
        self.assertEqual(messages[1]["content"], self.instructions.decode())
        if messages[-2]["role"] != "tool":
            return tool_reply(f"inspect_uploads-{self.model_calls}", "inspect_uploads", {})
        response = json.loads(tool_reply(f"prepare_upload-{self.model_calls}", "prepare_upload", {"upload_ref": UPLOAD_REF}))
        function = response["choices"][0]["message"]["tool_calls"][0]["function"]
        options = dict(self.preparation_options[-1])
        if self.correction_feedback is not None:
            messages = json.loads(raw)["messages"]
            feedback = messages[-2]["content"]
            if not feedback.startswith("{"):
                self.assertEqual(1, len(self.source_clients))
                self.assertTrue(self.source_clients[0][1].confirmed)
                self.assertEqual((self.worker.preparations, self.worker.executions), (0, 0))
                self.assertIn(self.correction_feedback, feedback.lower())
            else:
                options = dict(self.preparation_options[0])
        arguments = json.loads(function["arguments"])
        arguments.update(options, output={"verbosity": "max", "shift_significant_digits": MAX_SHIFT_SIGNIFICANT_DIGITS})
        function["arguments"] = json.dumps(arguments)
        return json.dumps(response).encode()
