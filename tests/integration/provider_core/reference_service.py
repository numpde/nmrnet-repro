"""Independent original populations through the existing supplied-Upload service.

Numerical authorities are the pinned 001 reference and 002 production packets.
The exporter supplies inputs only; new predictions never become expectations.
"""

import copy
import hashlib
import json
import os
from pathlib import Path

from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.native_lifecycle import require_host_source_closure
from tests.integration.nmrnet.normative.test_nmrnet_supplied_lifecycle import SuppliedLifecycle
from tests.integration.nmrnet.reference_fixtures import (
    PRODUCTION_SHA256,
    REFERENCE_SHA256,
    pinned_packet,
    require_reference_inputs,
    require_reference_predictions,
    verify_fixture,
)
from tests.integration.nmrnet.resource_evidence import require_cgroup_resources


class ExportedServiceLifecycle(SuppliedLifecycle):
    """A completed private producer hands exact source bytes to the Upload route."""

    input_kind = "upload"
    expected_model_calls = 2

    def load_case(self):
        with Path("/input/scientific/input.xyz").open("rb") as stream:
            self.source = stream.read(1024 * 1024 + 1)
        with Path("/input/scientific/manifest.json").open("rb") as stream:
            manifest_bytes = stream.read(1024 * 1024 + 1)
        self.assertLessEqual(len(self.source), 1024 * 1024)
        self.assertLessEqual(len(manifest_bytes), 1024 * 1024)
        self.manifest = json.loads(manifest_bytes)
        producer = self.manifest["producer"]
        require_host_source_closure(producer["execution"], Path("/input/sources.sha256").read_bytes())
        self.assertEqual(producer["execution"]["image_id"], os.environ["NMRNET_QUALIFICATION_IMAGE_ID"])
        self.assertEqual(producer["execution"]["repository_revision"], os.environ["NMRNET_QUALIFICATION_REPOSITORY_REVISION"])
        self.assertEqual(producer["upstream_revision"], os.environ["NMRNET_QUALIFICATION_UPSTREAM_REVISION"])
        self.assertEqual(
            producer["resource_scope"], "cgroup_through_input_rendering_snapshot_before_serialization_and_output_writes"
        )
        require_cgroup_resources(producer["resources"], memory_bytes=1073741824)
        self.manifest_identity = {"size": len(manifest_bytes), "sha256": hashlib.sha256(manifest_bytes).hexdigest()}
        self.assertEqual(self.manifest["case"], self.scientific_case)
        verify_fixture(self.source, self.manifest)
        records = self.manifest["records"]
        self.expected_records = tuple(range(1, len(records) + 1))
        self.expected_positions = tuple(tuple(item["source_positions"]) for item in records)
        self.expected = [
            {"atoms": item["original_request"]["atoms"], "coordinates_angstrom": item["original_request"]["coordinates"]}
            for item in records
        ]
        self.fixture_sha256 = hashlib.sha256(self.source).hexdigest()
        self.oracle_sha256 = self.manifest_identity["sha256"]
        selection = ", ".join(str(number) for number in self.expected_records)
        self.instructions = (
            f"Predict XYZ Upload {UPLOAD_REF}, records {selection} in that order; coordinates are angstroms.".encode()
        )

    def fixture_sources(self):
        return [
            ("tests/integration/provider_core/reference_fixtures.py", Path(__file__).with_name("reference_fixtures.py")),
            ("tests/integration/provider_core/reference_service.py", Path(__file__)),
        ]

    def qualification_details(self, content):
        return {
            **super().qualification_details(content),
            "input_manifest": self.manifest_identity,
            "producer": self.manifest["producer"],
        }


class ReferenceServiceLifecycle(ExportedServiceLifecycle):
    """The existing Upload source/scientific joins with independent reference values."""

    scientific_case = "references"

    async def model_reply(self, request, raw):
        response = json.loads(await super().model_reply(request, raw))
        call = response["choices"][0]["message"]["tool_calls"][0]["function"]
        if call["name"] == "prepare_upload":
            arguments = json.loads(call["arguments"])
            arguments["output"] = {"verbosity": "max", "shift_significant_digits": 17}
            call["arguments"] = json.dumps(arguments)
        return json.dumps(response).encode()

    def load_case(self):
        super().load_case()
        self.instructions += b" Return shifts with 17 significant digits for exact comparison to the pinned numerical reference."
        self.reference = pinned_packet("/input/reference-cpu-float32.json", REFERENCE_SHA256)
        self.production = pinned_packet("/input/prediction-cpu-float32.json", PRODUCTION_SHA256)
        require_reference_inputs(self.manifest, self.reference, self.production)
        self.assertEqual(
            [(item["source_element"], item["source_key_hex"]) for item in self.manifest["records"]],
            [("H", "35333738"), ("H", "35333739"), ("C", "30"), ("C", "31")],
        )
        self.oracle_sha256 = PRODUCTION_SHA256

    def fixture_sources(self):
        return [
            *super().fixture_sources(),
            ("artifacts/reference-cpu-float32.json", Path("/input/reference-cpu-float32.json")),
            ("artifacts/prediction-cpu-float32.json", Path("/input/prediction-cpu-float32.json")),
        ]

    def assert_result(self, terminal):
        result = super().assert_result(terminal)
        rows = [item["predictions"] for item in result["geometries"]]
        self.population = require_reference_predictions(self.manifest["records"], rows, self.reference, self.production)
        # Mutating the accepted result exercises this exact gate without another
        # reader/model invocation or regenerated expected predictions.
        for change in ("shift", "atom", "nucleus", "producer", "missing"):
            altered_rows, altered_records = copy.deepcopy(rows), copy.deepcopy(self.manifest["records"])
            if change == "shift":
                altered_rows[0][0]["shift_ppm"] += 1
            elif change == "atom":
                altered_rows[0][0]["atom_index"] = 127
            elif change == "nucleus":
                altered_rows[0][0]["element"] = "H"
            elif change == "producer":
                altered_records[0]["source_key_hex"] = "00"
            else:
                altered_rows[0].pop()
            with self.subTest(control=change), self.assertRaises(AssertionError):
                require_reference_predictions(altered_records, altered_rows, self.reference, self.production)
        return result

    def qualification_details(self, content):
        return {
            **super().qualification_details(content),
            "reference_population": self.population,
            "original_records": [
                {key: item[key] for key in ("source_element", "source_key_hex", "raw_sha256", "original_request_identity")}
                for item in self.manifest["records"]
            ],
            "reference_packet_sha256": REFERENCE_SHA256,
            "production_packet_sha256": PRODUCTION_SHA256,
            "numerical_policy": {"atol": 0, "rtol": 0},
            "scientific_fixture": "four_original_references",
            "negative_controls": ["shift", "atom", "nucleus", "producer", "missing"],
        }
