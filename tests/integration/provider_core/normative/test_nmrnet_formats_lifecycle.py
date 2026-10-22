"""The closed native-format profile retains authored, independent input oracles."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tests.fakes.job_input import facts, response
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.uploads import UPLOAD_REF
from tests.integration.nmrnet.formats_service import FORMAT_CASES, FormatsServiceLifecycle
from tests.integration.nmrnet.interpreter_ports import chat_endpoint
from tests.integration.nmrnet.normative.test_nmrnet_supplied_lifecycle import SuppliedLifecycle
from tests.integration.nmrnet.smiles_ports import InputPorts
from tests.integration.nmrnet.test_nmrnet_session import HostOwner

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.interpreter import InputInterpreter
from nmrnet_provider.pdb import prepare_pdb
from nmrnet_provider.process_reports import failure_evidence
from provider_core.job_input import verify_job_input
from provider_core.upload_response import UploadMember, UploadSet


class FormatPreparationBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdf_correction_uses_distinct_tool_ids_and_fresh_readers_with_one_source_fetch(self):
        class Fixture(FormatsServiceLifecycle, unittest.TestCase):
            pass

        fixture = Fixture()
        fixture.fixture_root = Path("/fixtures/preparation")
        fixture.format_case = "sdf-correction"
        fixture.load_case()
        fixture.model_calls, fixture.source_clients = 0, []
        fixture.worker = SimpleNamespace(preparations=0, executions=0)
        ports = InputPorts()
        ports.instructions = verify_job_input(response(fixture.instructions), feed=facts(fixture.instructions))
        ports.uploads.inspect.return_value = UploadSet(
            "job:example", (UploadMember(UPLOAD_REF, "Authored SDF", len(fixture.source), None),)
        )
        ports.uploads.acquire.return_value = SimpleNamespace(
            job_ref="job:example",
            upload_ref=UPLOAD_REF,
            data=fixture.source,
            byte_length=len(fixture.source),
            content_hash="sha256:" + fixture.fixture_sha256,
        )
        geometry = ports.reader.read.return_value[0]
        readers = [
            SimpleNamespace(
                read=AsyncMock(side_effect=InputRejected("Cannot prepare SDF record 4: no M  END terminator.")),
                close=Mock(),
                wait_closed=AsyncMock(),
            ),
            SimpleNamespace(
                read=AsyncMock(
                    return_value=(replace(geometry, source_record_number=3), replace(geometry, source_record_number=1))
                ),
                close=Mock(),
                wait_closed=AsyncMock(),
            ),
        ]
        hosts = [HostOwner(), HostOwner()]

        async def allocate():
            index = len(fixture.source_clients)
            pair = readers[index], hosts[index]
            fixture.source_clients.append(pair)
            return pair

        ports.allocate_reader.side_effect = allocate
        identifiers = []

        async def reply(headers, raw):
            body = await fixture.model_reply(headers, raw)
            identifiers.append(json.loads(body)["choices"][0]["message"]["tool_calls"][0]["id"])
            return body

        async with TlsPeer("success", response_factory=reply) as peer:
            chooser = InputInterpreter((chat_endpoint(peer),), max_turns=6)
            owner = ports.owner(chooser.run, chooser=chooser)
            try:
                prepared = await owner.prepare(ports.instructions)
            except Exception as cause:
                self.fail(json.dumps(failure_evidence(cause, secrets=("PRIVATE_MODEL_KEY",))))
            finally:
                await owner.retire()
        self.assertEqual(len(identifiers), 3)
        self.assertEqual(len(set(identifiers)), 3)
        self.assertEqual(readers[0].read.call_args.kwargs["selection"], "all")
        self.assertEqual(readers[1].read.call_args.kwargs["selection"], (3, 1))
        self.assertEqual([item.source_record_number for item in prepared.geometries], [3, 1])
        self.assertTrue(all(host.confirmed for host in hosts))
        ports.uploads.acquire.assert_awaited_once_with(UPLOAD_REF)
        ports.allocate_reader.assert_awaited()
        self.assertEqual(ports.allocate_reader.await_count, 2)

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
                        hosts = [HostOwner(), HostOwner()]
                        for host in hosts:
                            host.retire = AsyncMock(wraps=host.retire)
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
    def test_supplied_sdf_keeps_both_distinct_carbon_only_geometries_without_hydrogen_expansion(self):
        fixture = FormatsServiceLifecycle()
        fixture.fixture_root = Path("/fixtures/preparation")
        fixture.format_case = "sdf-carbon-only"
        fixture.load_case()
        self.assertEqual(fixture.relative_source, "normative/butane_conformers.sdf")
        self.assertEqual(fixture.expected_records, (1, 2))
        self.assertEqual(fixture.expected_positions, ((1, 2, 3, 4), (1, 2, 3, 4)))
        self.assertEqual(fixture.reader_options["selection"], "all")
        self.assertEqual([geometry["atoms"] for geometry in fixture.expected], [["C"] * 4, ["C"] * 4])
        self.assertNotEqual(fixture.expected[0]["coordinates_angstrom"], fixture.expected[1]["coordinates_angstrom"])
        self.assertEqual((fixture.expected_preparation_calls, fixture.expected_model_calls), (1, 2))

    def test_fixed_format_selector_defaults_to_all_cases_and_preserves_an_explicit_subset(self):
        for selection, expected in (
            ("", FORMAT_CASES),
            ("sdf-correction pdb-models pdb-alternate mol2", ("sdf-correction", "pdb-models", "pdb-alternate", "mol2")),
            ("sdf-carbon-only", ("sdf-carbon-only",)),
        ):
            with self.subTest(selection=selection):
                result = subprocess.run(
                    [
                        "bash",
                        "-euc",
                        'source "$1"; provider_service_select_formats "$2"; printf "%s\\0" "${provider_service_format_cases[@]}"',
                        "formats",
                        "/workspace/tools/provider_service_qualification.sh",
                        selection,
                    ],
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                self.assertEqual(result.stdout.split(b"\0")[:-1], [name.encode() for name in expected])

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

    def test_cases_keep_known_geometry_populations_and_source_selection(self):
        self.assertEqual(
            FORMAT_CASES,
            (
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
            ),
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
        self.assertEqual(counts, [1, 1, 2, 1, 3, 2, 2, 1, 2, 2])
        self.assertEqual(formats, {"json", "table", "xyz", "mol", "sdf", "pdb", "mol2"})
        self.assertEqual(reader_calls, 12)
        self.assertEqual(len(FORMAT_CASES) + reader_calls, 22)
