"""Fixed supplied-format fixtures for the existing native service qualification.

The authored expected.json owns coordinates and source correspondence. Scripted
tool choices exercise reading, not language-model understanding or NMR accuracy.
"""

import hashlib
import json
import struct
from decimal import Decimal
from pathlib import Path

from tests.integration.nmrnet.normative.test_nmrnet_supplied_lifecycle import (
    NM_TABLE,
    SuppliedLifecycle,
    nm_conversion_oracle,
)

FORMAT_CASES = ("json", "table", "xyz", "mol", "sdf-all", "sdf-correction", "pdb-models", "pdb-alternate", "mol2")


class FormatsServiceLifecycle(SuppliedLifecycle):
    """Assemble the nine closed fixtures; normal service/factory owners execute them."""

    input_kind = "upload"
    fixture_root = Path("/input/preparation")

    def expected_reader_options(self):
        return self.reader_options

    def load_case(self):
        oracle = (self.fixture_root / "expected.json").read_bytes()
        corpus = json.loads(oracle)
        self.oracle_sha256 = hashlib.sha256(oracle).hexdigest()
        self.expected_preparation_calls = 2 if self.format_case in ("sdf-correction", "pdb-alternate") else 1
        self.expected_model_calls = 1 + self.expected_preparation_calls
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
            source_format, relative_source, records = {
                "mol": ("mol", "normative/atom_order_v2000.mol", (1,)),
                "sdf-all": ("sdf", "normative/mixed_records.sdf", (1, 2, 3)),
                "sdf-correction": ("sdf", "adversarial/invalid_last.sdf", (3, 1)),
                "pdb-models": ("pdb", "normative/models.pdb", (2, 1)),
                "mol2": ("mol2", "normative/mixed_records.mol2", (2, 1)),
            }[self.format_case]
            self.relative_source = relative_source
            native = corpus["native_files"][
                "normative/mixed_records.sdf" if self.format_case == "sdf-correction" else relative_source
            ]
            self.expected_records = records
            self.expected = [corpus["geometries"][native["geometry_names"][record - 1]] for record in records]
            self.expected_positions = tuple(tuple(native["source_atom_ids"][record - 1]) for record in records)
            selection = None if source_format == "mol" else "all" if self.format_case == "sdf-all" else records
            self.reader_options.update(source_format=source_format, selection=selection)
        if self.relative_source is not None:
            self.source = (self.fixture_root / self.relative_source).read_bytes()
        self.fixture_sha256 = hashlib.sha256(self.source).hexdigest()
        options = self.reader_options
        self.instructions = (
            f"Predict the attached {self.format_case} fixture as {options['source_format']}, source unit {options['source_unit']}; "
            f"select {options['selection']!r} in that order, alternate location {options['alternate_location']!r}. "
            "Keep supplied atoms and coordinates. Return shifts with 17 significant digits."
        ).encode()

    def fixture_sources(self):
        sources = [
            ("tests/integration/provider_core/formats_service.py", Path(__file__)),
            ("tests/fixtures/preparation/expected.json", self.fixture_root / "expected.json"),
            ("tests/fixtures/preparation/README.txt", self.fixture_root / "README.txt"),
        ]
        if self.relative_source is not None:
            sources.append(("tests/fixtures/preparation/" + self.relative_source, self.fixture_root / self.relative_source))
        return sources

    def retain_allocation_intent(self, allocation):
        """Join a qualification request to this case's admitted Job, not new authority."""
        body = (
            json.dumps(
                {
                    "case": self.format_case,
                    "job_ref": self.selected_input.job_ref,
                    "input_fingerprint": self.selected_input.input_fingerprint,
                    "fixture_source_sha256": self.fixture_sha256,
                    "fixture_source_bytes": len(self.source),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n"
        )
        selection = self.state_root / "selection.json"
        if selection.exists():
            self.assertEqual(selection.read_bytes(), body)
        else:
            with selection.open("xb") as stream:
                stream.write(body)
            with (self.state_root / "admission-events.json").open("x") as stream:
                json.dump(self.events, stream)
        with (self.state_root / "allocation-intents" / allocation.allocation).open("xb") as stream:
            stream.write((self.state_root / "selected").read_bytes())
            stream.write(body)

    async def model_reply(self, request, raw):
        response = json.loads(await super().model_reply(request, raw))
        function = response["choices"][0]["message"]["tool_calls"][0]["function"]
        if function["name"] != "prepare_upload":
            return json.dumps(response).encode()
        options = dict(self.reader_options)
        if self.expected_preparation_calls == 2:
            messages = json.loads(raw)["messages"]
            feedback = json.loads(messages[-1]["content"])
            if "rejected" in feedback:
                self.assertEqual(len(self.source_clients), 1)
                self.assertTrue(self.source_clients[0][1].confirmed)
                self.assertEqual((self.worker.preparations, self.worker.executions), (0, 0))
                self.assertIn("record 4" if self.format_case == "sdf-correction" else "alternate", feedback["rejected"].lower())
            elif self.format_case == "sdf-correction":
                options["selection"] = "all"
            else:
                options["alternate_location"] = None
        arguments = json.loads(function["arguments"])
        arguments.update(options, shift_significant_digits=17)
        function["arguments"] = json.dumps(arguments)
        return json.dumps(response).encode()

    def assert_result(self, terminal):
        result = super().assert_result(terminal)
        self.assertEqual(result["output"], {"shift_significant_digits": 17})
        self.assertEqual(result["interpretation"]["corrections"], self.expected_preparation_calls - 1)
        evidence = json.loads(terminal.evidence.content)["geometries"]
        for public, private, expected in zip(result["geometries"], evidence, self.expected, strict=True):
            self.assertEqual(public["predictions"], private["prediction"]["predictions"])
            self.assertEqual(
                public["float32_coordinates"],
                [[struct.unpack("!f", struct.pack("!f", value))[0] for value in row] for row in expected["coordinates_angstrom"]],
            )
        return result
