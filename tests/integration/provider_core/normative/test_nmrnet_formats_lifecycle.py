"""The closed native-format profile retains authored, independent input oracles."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.formats_service import FORMAT_CASES, FormatsServiceLifecycle
from tests.integration.nmrnet.normative.test_nmrnet_supplied_lifecycle import SuppliedLifecycle
from tests.integration.nmrnet.smiles_ports import InputPorts

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.pdb import prepare_pdb


class FormatPreparationBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_config_keeps_one_call_normally_and_two_for_correction_cases(self):
        for name, expected in (("json", 1), ("sdf-correction", 2), ("pdb-alternate", 2)):
            with self.subTest(case=name):
                fixture = FormatsServiceLifecycle()
                fixture.fixture_root = Path("/fixtures/preparation")
                fixture.format_case = name
                fixture.load_case()
                with tempfile.TemporaryDirectory() as directory:
                    fixture.state_root = Path(directory)
                    async with fixture.configured_peers():
                        settings = fixture.config.preparation
                        self.assertEqual(settings.max_worker_calls, expected)
                        if name != "pdb-alternate":
                            continue
                        ports = InputPorts()
                        ports.uploads.acquire.return_value = SimpleNamespace(
                            upload_ref=UPLOAD_REF,
                            job_ref="job:example",
                            byte_length=len(fixture.source),
                            content_hash="sha256:" + fixture.fixture_sha256,
                            data=fixture.source,
                        )

                        async def read(source, **options):
                            return prepare_pdb(
                                source,
                                source_unit=options["source_unit"],
                                selection=options["selection"],
                                alternate_location=options["alternate_location"],
                            )

                        readers = [
                            SimpleNamespace(read=AsyncMock(side_effect=read), close=Mock(), wait_closed=AsyncMock())
                            for _ in range(2)
                        ]
                        hosts = [SimpleNamespace(retire=AsyncMock()) for _ in range(2)]
                        ports.allocate_reader.side_effect = list(zip(readers, hosts, strict=True))

                        # silent-defaults: allow-contract -- Bind the two recording retirement ports for this fixed correction case.
                        async def choose(_instructions, capability, *, hosts=hosts):
                            with self.assertRaisesRegex(InputRejected, "alternate"):
                                await capability.prepare_upload(
                                    UPLOAD_REF, source_format="pdb", source_unit="angstrom", selection=(1,)
                                )
                            hosts[0].retire.assert_awaited_once()
                            await capability.prepare_upload(
                                UPLOAD_REF, source_format="pdb", source_unit="angstrom", selection=(1,), alternate_location="B"
                            )

                        owner = ports.owner(choose, max_worker_calls=settings.max_worker_calls, seconds=settings.seconds)
                        self.addAsyncCleanup(owner.retire)
                        prepared = await owner.prepare(ports.instructions)
                        self.assertEqual(len(prepared.geometries), 1)
                        self.assertEqual(ports.allocate_reader.await_count, 2)
                        ports.uploads.acquire.assert_awaited_once()
                        for reader, host in zip(readers, hosts, strict=True):
                            reader.read.assert_awaited_once()
                            reader.close.assert_called_once()
                            host.retire.assert_awaited_once()


class SuppliedFormatFixtureTests(unittest.TestCase):
    def test_host_admits_only_the_case_addressed_actual_allocation_intent(self):
        fixture = FormatsServiceLifecycle()
        fixture.fixture_root = Path("/fixtures/preparation")
        fixture.format_case = "json"
        fixture.load_case()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture.state_root = root / "formats" / "json"
            (fixture.state_root / "allocation-intents").mkdir(parents=True)
            (root / "active-case").write_bytes(b"json\n")
            (fixture.state_root / "admitted").write_bytes(b"admitted\n")
            fingerprint = "sha256:" + "1" * 64
            (fixture.state_root / "selected").write_bytes(fingerprint.encode() + b"\n")
            fixture.selected_input = SimpleNamespace(job_ref="job:fixture", input_fingerprint=fingerprint)
            fixture.events = ["inventory", "hello", "inventory", "jobs", "factory"]
            token = "a" * 32
            fixture.retain_allocation_intent(SimpleNamespace(allocation=token))
            selection = json.loads((fixture.state_root / "selection.json").read_bytes())
            self.assertEqual(selection["fixture_source_sha256"], hashlib.sha256(fixture.source).hexdigest())
            self.assertEqual(selection["input_fingerprint"], fingerprint)
            self.assertEqual(selection["job_ref"], "job:fixture")
            result = subprocess.run(
                [
                    "bash",
                    "-eu",
                    "-c",
                    'source "$1"; provider_service_format_request "$2" "$3"',
                    "formats",
                    "/workspace/tools/provider_service_qualification.sh",
                    str(root),
                    token,
                ],
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(result.stdout, b"")

    def test_shared_upload_expectation_uses_the_final_subclass_record_population(self):
        fixture = SuppliedLifecycle()
        fixture.input_kind = "upload"
        fixture.expected_records = (2, 1)
        self.assertEqual(fixture.expected_reader_options()["selection"], (2, 1))
        # The existing reference/capacity fixtures replace the initial XYZ
        # population after parent setup. Their selected record IDs stay authoritative.
        fixture.expected_records = (1, 2, 3, 4)
        self.assertEqual(
            fixture.expected_reader_options(),
            {"source_format": "xyz", "source_unit": "angstrom", "selection": (1, 2, 3, 4), "alternate_location": None},
        )

    def test_nine_cases_keep_known_geometry_populations_and_source_selection(self):
        self.assertEqual(
            FORMAT_CASES, ("json", "table", "xyz", "mol", "sdf-all", "sdf-correction", "pdb-models", "pdb-alternate", "mol2")
        )
        counts, reader_calls, formats = [], 0, set()
        for name in FORMAT_CASES:
            with self.subTest(case=name):
                fixture = FormatsServiceLifecycle()
                fixture.fixture_root = Path("/fixtures/preparation")
                fixture.format_case = name
                fixture.load_case()
                counts.append(len(fixture.expected))
                reader_calls += fixture.expected_preparation_calls
                formats.add(fixture.reader_options["source_format"])
                self.assertEqual(len(fixture.expected_records), len(fixture.expected))
                self.assertEqual(len(fixture.expected_positions), len(fixture.expected))
                self.assertEqual(fixture.fixture_sha256, hashlib.sha256(fixture.source).hexdigest())
                if name == "json":
                    expected = fixture.expected[0]
                    self.assertEqual(
                        json.loads(fixture.source),
                        {
                            "atoms": expected["atoms"],
                            "coordinates": expected["coordinates_angstrom"],
                            "coordinate_unit": "angstrom",
                        },
                    )
                if name in ("xyz", "pdb-models", "mol2"):
                    self.assertEqual(fixture.expected_records, (2, 1))
                if name == "sdf-correction":
                    self.assertEqual(fixture.expected_records, (3, 1))
                    self.assertEqual(fixture.relative_source, "adversarial/invalid_last.sdf")
                if name == "pdb-alternate":
                    self.assertEqual(fixture.expected_positions, ((40, 7, 23, 5, 91),))
                    self.assertEqual(fixture.expected[0]["coordinates_angstrom"][2], [0.125, 0, 0])
                    self.assertEqual(fixture.reader_options["alternate_location"], "B")
        self.assertEqual(counts, [1, 1, 2, 1, 3, 2, 2, 1, 2])
        self.assertEqual(formats, {"json", "table", "xyz", "mol", "sdf", "pdb", "mol2"})
        self.assertEqual(reader_calls, 11)
        self.assertEqual(len(FORMAT_CASES) + reader_calls, 20)
