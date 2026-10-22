"""Supplied molecular coordinates through the same configured native lifecycle."""

import asyncio
import base64
import hashlib
import json
import math
import os
import sys
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from tests.fakes.conversation import call
from tests.fakes.job_input import facts
from tests.fakes.uploads import UPLOAD_REF, encoded, grant, member, membership
from tests.integration.nmrnet.native_lifecycle import NativeLifecycle

from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.smiles import GenerationSettings

# Original authored chloromethane, H/Cl/C/H/H, expressed in nm. Expected angstrom
# numbers are the independent literals in expected.json, not reader output.
NM_TABLE = b"H 0.1027 0 -0.0363\nCl 0 0 0.178\nC 0 0 0\nH -0.0514 0.089 -0.0363\nH -0.0514 -0.089 -0.0363\n"
PREFIX = "é: supplied coordinates in nm\n".encode()


def nm_conversion_oracle(source_nm, intended_angstrom):
    """Bound two binary64 roundings using fixed, independent decimal literals.

    Source parsing contributes 10 times its exact representational error.
    Multiplication by the exactly representable 10 contributes at most half
    an ULP of the rounded exact binary product. Fraction arithmetic keeps this
    budget and the intended angstrom value exact; no worker output sets it.
    The unit's exact factor is documented in input_geometry.coordinate_conversion.
    """
    source, intended = Fraction(source_nm), Fraction(intended_angstrom)
    if source * 10 != intended:
        raise AssertionError("Fixed nm and angstrom fixture literals disagree about the exact times-ten conversion")
    if source == 0:
        return intended, Fraction(0)
    parsed = Fraction.from_float(float(source))
    parse_error = abs(parsed - source) * 10
    product_rounding = Fraction.from_float(math.ulp(float(parsed * 10))) / 2
    return intended, parse_error + product_rounding


class ObservedReader(GeometryClient):
    async def read(self, source, **options):
        self.calls += 1
        self.source, self.options = source, options
        self.returned = await super().read(source, **options)
        return self.returned


class SuppliedLifecycle(NativeLifecycle):
    """Route-specific source selection and independent supplied-coordinate oracle."""

    generation_settings = GenerationSettings(seed=17, max_iterations=200, timeout_seconds=1)

    def load_case(self):
        oracle = Path("/input/expected.json").read_bytes()
        geometries = json.loads(oracle)["geometries"]
        self.oracle_sha256 = hashlib.sha256(oracle).hexdigest()
        if self.input_kind == "pasted":
            self.source = NM_TABLE
            self.instructions = PREFIX + self.source + b"Predict these supplied atoms."
            self.expected_names = ("chloromethane",)
            self.expected_records, self.expected_positions = (1,), ((1, 2, 3, 4, 5),)
            self.expected_model_calls = 1
            self.assertEqual((sys.float_info.radix, sys.float_info.mant_dig, sys.float_info.rounds), (2, 53, 1))
            exact = json.loads(oracle, parse_float=Decimal)["geometries"]["chloromethane"]
            rows = [row.split() for row in self.source.decode().splitlines()]
            self.assertEqual([row[0] for row in rows], exact["atoms"])
            self.coordinate_oracle = [
                [nm_conversion_oracle(value, intended) for value, intended in zip(row[1:], target, strict=True)]
                for row, target in zip(rows, exact["coordinates_angstrom"], strict=True)
            ]
        else:
            self.source = Path("/input/mixed_records.xyz").read_bytes()
            self.instructions = f"Predict XYZ Upload {UPLOAD_REF}, records 2 then 1, in angstroms.".encode()
            self.expected_names = ("water", "chloromethane")
            self.expected_records, self.expected_positions = (2, 1), ((10, 11, 12), (3, 4, 5, 6, 7))
            self.expected_model_calls = 2
        self.expected = [geometries[name] for name in self.expected_names]
        self.fixture_sha256 = hashlib.sha256(self.source).hexdigest()

    def execution_sources(self):
        sources = [
            ("tests/integration/provider_worker/geometry_probe.py", Path("/source/structure_probe.py")),
            ("tests/fixtures/preparation/expected.json", Path("/input/expected.json")),
        ]
        if self.input_kind == "upload":
            sources.append(("tests/fixtures/preparation/normative/mixed_records.xyz", Path("/input/mixed_records.xyz")))
        return sources

    async def allocate_reader(self):
        self.assertFalse(hasattr(self, "geometry_client"), "This fixture permits one reader allocation")
        allocation = os.environ["NMRNET_QUALIFICATION_GEOMETRY_ALLOCATION"]
        self.assertEqual(Path("/geometry-ipc/ready").read_bytes(), (allocation + "\n").encode())
        reader, writer = await asyncio.open_unix_connection("/geometry-ipc/geometry.sock")
        self.geometry_client = ObservedReader(reader, writer, allocation=allocation, operation_seconds=10)
        self.geometry_client.calls = 0
        return self.geometry_client, self.geometry_host

    async def allocate_generator(self, **_settings):
        raise AssertionError("Supplied input must not allocate a generator or add atoms")

    async def model_reply(self, request, raw):
        self.assertIn(b"authorization: bearer private_model_key\r\n", request.lower())
        self.assertNotIn(b"signature:", request.lower())
        self.model_calls += 1
        messages = json.loads(raw)["messages"]
        self.assertEqual(messages[1]["content"], self.instructions.decode())
        last = messages[-1]
        if self.input_kind == "pasted":
            name, arguments = (
                "prepare_pasted",
                {
                    "source_text": self.source.decode("utf-8"),
                    "source_format": "table",
                    "source_unit": "nm",
                },
            )
        elif last["role"] != "tool":
            name, arguments = "inspect_uploads", {}
        else:
            name, arguments = (
                "prepare_upload",
                {
                    "upload_ref": UPLOAD_REF,
                    "source_format": "xyz",
                    "source_unit": "angstrom",
                    "selection": list(self.expected_records),
                    "alternate_location": None,
                },
            )
        return json.dumps({"choices": [{"message": call(name, name, arguments), "finish_reason": "tool_calls"}]}).encode()

    async def upload_reply(self, method, path, _body):
        self.assertEqual(self.input_kind, "upload")
        digest = "sha256:" + self.fixture_sha256
        if path.endswith("/uploads"):
            self.assertEqual(method, "GET")
            self.counts["membership"] += 1
            return encoded(membership(uploads=[member(byte_length=len(self.source), content_hash=digest)]))
        self.assertEqual(method, "POST")
        self.assertTrue(path.endswith("/read-capability"))
        self.counts["grant"] += 1
        return encoded(
            grant(
                byte_length=len(self.source),
                content_hash=digest,
                download_url=self.store_peer.origin + "/upload/v1/uploads/" + UPLOAD_REF + "/bytes",
                expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
            )
        )

    def qualification_details(self, content):
        details = {
            "reader_calls": self.geometry_client.calls,
            "source_sha256": self.fixture_sha256,
            "oracle_sha256": self.oracle_sha256,
            "returned_records": [item.source_record_number for item in self.geometry_client.returned],
            "reader_options": self.geometry_client.options,
            "request_sha256": [item["request_sha256"] for item in json.loads(content)["geometries"]],
        }
        if self.input_kind == "pasted":
            details["conversion_error_bound_angstrom"] = [
                [str(bound) for _intended, bound in row] for row in self.coordinate_oracle
            ]
        return details

    def expected_reader_options(self):
        if self.input_kind == "pasted":
            return {"source_format": "table", "source_unit": "nm", "selection": None}
        return {"source_format": "xyz", "source_unit": "angstrom", "selection": self.expected_records, "alternate_location": None}

    def assert_result(self, terminal):
        self.assertEqual(self.geometry_client.source, self.source)
        options = self.expected_reader_options()
        self.assertEqual(self.geometry_client.options, options)
        result = json.loads(base64.b64decode(json.loads(terminal.body)["canonical_result_base64"], validate=True))
        self.assertEqual(result["execution"], self.execution)
        self.assertEqual(result["selected_models"], self.artifacts)
        self.assertEqual(result["runtime"], self.scientific_runtime)
        identity = json.loads(terminal.attempt.prepared_identity.content)
        self.assertEqual(identity, json.loads(self.worker.prepared_identity.content))
        self.assertEqual(identity["interpretation"], result["interpretation"])
        evidence = json.loads(terminal.evidence.content)["geometries"]
        self.assertEqual(len(result["geometries"]), len(self.expected))
        for geometry, observed, prepared, expected, record_number, positions in zip(
            result["geometries"],
            evidence,
            self.geometry_client.returned,
            self.expected,
            self.expected_records,
            self.expected_positions,
            strict=True,
        ):
            request = geometry["prepared_request"]
            self.assertEqual(request, json.loads(prepared.request_bytes))
            self.assertEqual(request["atoms"], expected["atoms"])
            self.assertEqual(request["coordinate_unit"], "angstrom")
            if options["source_unit"] == "nm":
                for actual_row, oracle_row in zip(request["coordinates"], self.coordinate_oracle, strict=True):
                    for actual, (intended, bound) in zip(actual_row, oracle_row, strict=True):
                        self.assertLessEqual(abs(Fraction.from_float(actual) - intended), bound)
            else:
                self.assertEqual(request["coordinates"], expected["coordinates_angstrom"])
            self.assertEqual(geometry["source"]["record_number"], record_number)
            self.assertEqual(geometry["source"]["positions"], list(positions))
            self.assertEqual(geometry["source"]["sha256"], self.fixture_sha256)
            digest = hashlib.sha256(prepared.request_bytes).hexdigest()
            self.assertEqual(geometry["request_sha256"], digest)
            self.assertEqual(observed["request_sha256"], digest)
            self.assertEqual(geometry["preparation"]["scale_to_angstrom"], 10 if options["source_unit"] == "nm" else 1)
            self.assertEqual(geometry["preparation"]["source_unit"], options["source_unit"])
            heads = {atom for atom in expected["atoms"] if atom in ("H", "C")}
            self.assertEqual(set(geometry["loaded_elements"]), heads)
            self.assertEqual(
                observed["provenance"]["artifacts"],
                {
                    "dictionary": self.artifacts["dictionary"],
                    "elements": {head: self.artifacts["elements"][head] for head in heads},
                },
            )
            self.assertEqual(observed["provenance"]["runtime"], self.scientific_runtime)
            self.assertEqual(
                [(item["atom_index"], item["element"]) for item in geometry["predictions"]],
                [(index, atom) for index, atom in enumerate(expected["atoms"]) if atom in heads],
            )
            self.assertTrue(all(math.isfinite(item["shift_ppm"]) for item in geometry["predictions"]))
            if self.input_kind == "pasted":
                self.assertEqual(geometry["source"]["origin"], "interpreter_supplied_coordinates")
                self.assertEqual(geometry["source"]["positions_relative_to"], "received_source_text")
                self.assertEqual(geometry["source"]["input_fingerprint"], facts(self.instructions).input_fingerprint)
            else:
                self.assertEqual(geometry["source"]["upload_ref"], UPLOAD_REF)
                self.assertEqual(geometry["source"]["byte_length"], len(self.source))
        if self.input_kind == "upload":
            self.assertEqual(len(self.store_peer.requests), 1)
            request = self.store_peer.requests[0]
            headers = {
                name.lower(): value.strip()
                for line in request.split(b"\r\n")[1:]
                if b":" in line
                for name, value in [line.split(b":", 1)]
            }
            self.assertEqual(headers[b"authorization"], b"Bearer " + grant()["capability"].encode())
            self.assertNotIn(b"signature:", request.lower())
            self.assertNotIn(b"private_model_key", request.lower())
        return result


class LiveSuppliedLifecycleTests(SuppliedLifecycle, unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.environ.get("NMRNET_LIVE_SUPPLIED_WORKER") == "pasted", "run the fixed scientific-worker/pasted lane")
    async def test_pasted_nm_coordinates_reach_retired_science_and_exact_replay(self):
        self.input_kind = "pasted"
        async with asyncio.timeout(150):
            await self.run_lifecycle()

    @unittest.skipUnless(
        os.environ.get("NMRNET_LIVE_SUPPLIED_WORKER") == "upload", "run the fixed scientific-worker/uploads lane"
    )
    async def test_uploaded_records_2_1_reach_retired_science_and_exact_replay(self):
        self.input_kind = "upload"
        async with asyncio.timeout(150):
            await self.run_lifecycle()
