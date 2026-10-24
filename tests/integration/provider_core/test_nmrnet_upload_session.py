"""Verified Upload selection reaches one retained, complete signed Job result.

The fixture invokes the pure XYZ parser in this container and records reader
retirement. Choice and science are recording ports; this establishes neither
model accuracy nor an external interpreter workflow.
"""

import asyncio
import base64
import copy
import hashlib
import json
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from tests.fakes.acquisition import store_headers
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import ATTEMPT_REF, complete_response, observation, progress_response, start_response
from tests.fakes.job_input import facts, response
from tests.fakes.publication import api
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.uploads import UPLOAD_REF, encoded, grant, member, membership

from nmrnet_provider.input_preparation import InputPreparation
from nmrnet_provider.retirement import retire_worker_channel
from nmrnet_provider.session import NMRNetSession
from nmrnet_provider.smiles import GenerationSettings
from nmrnet_provider.xyz import prepare_xyz
from provider_core.attempt_record import (
    MAX_RECORD_BYTES,
    AcknowledgedAttempt,
    LocalExecutionPhase,
    TerminalConfirmed,
    TerminalPending,
)
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.controller import AttemptController
from provider_core.ownership import AttemptClaims
from provider_core.transport import HttpTransport
from provider_core.uploads import JobUploads, UploadLimits

INSTRUCTIONS = f"Predict XYZ Upload {UPLOAD_REF}, records 2 then 1, in angstroms.".encode()


class Reader:
    """Exercise the actual parser on acquired bytes; record local closure."""

    def __init__(self):
        self.closed = False
        self.observed = False

    async def read(self, source, **options):
        if options.pop("source_format") != "xyz" or options.pop("alternate_location") is not None:
            raise AssertionError("This fixture chooses XYZ without PDB options")
        return prepare_xyz(source, **options)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.observed = True


class Retirement:
    confirmed = False

    async def retire(self):
        self.confirmed = True

    async def retire_channel(self, worker):
        return await retire_worker_channel(worker, self)


class Chooser:
    calls = 0

    async def retire(self):
        self.retired = True

    async def run(self, instructions, capability):
        if instructions.specification_bytes != INSTRUCTIONS:
            raise AssertionError("Chooser did not receive the original verified instructions")
        self.calls += 1
        inspected = await capability.inspect()
        if inspected.members[0].upload_ref != UPLOAD_REF:
            raise AssertionError("Chooser did not receive the inspected Upload reference")
        await capability.prepare_upload(UPLOAD_REF, source_format="xyz", source_unit="angstrom", selection=(2, 1))


class ScientificWorker:
    def __init__(self, test):
        self.test = test
        self.events = []
        self.handle = object()

    async def prepare(self, requests):
        self.test.assertTrue(self.test.reader_host.confirmed)
        self.test.assertTrue(self.test.reader.closed and self.test.reader.observed)
        self.events.append("prepare")
        self.requests = requests
        self.test.assertEqual([json.loads(raw) for raw in requests], self.test.expected)
        return self.handle

    async def execute(self, handle):
        self.test.assertIs(handle, self.handle)
        self.events.append("execute")
        (active,) = await self.test.store.records()
        self.test.assertIsInstance(active, AcknowledgedAttempt)
        self.test.assertIs(active.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
        self.test.candidate = json.loads(active.prepared_identity.content)
        self.test.assert_sources(self.test.candidate["geometries"])
        outcomes = []
        for raw, positions in zip(self.requests, ((1, 2), (0, 2, 3, 4)), strict=True):
            atoms = json.loads(raw)["atoms"]
            heads = {atoms[index] for index in positions}
            artifacts = {
                "dictionary": self.test.artifacts["dictionary"],
                "elements": {head: self.test.artifacts["elements"][head] for head in heads},
            }
            outcomes.append(
                {
                    "request_sha256": hashlib.sha256(raw).hexdigest(),
                    "prediction": {
                        "unit": "ppm",
                        "predictions": [
                            {"atom_index": index, "element": atoms[index], "shift_ppm": float(index + len(atoms))}
                            for index in positions
                        ],
                    },
                    "provenance": {"artifacts": copy.deepcopy(artifacts), "runtime": self.test.runtime},
                    "private_warnings": [],
                }
            )
        return tuple(outcomes)

    def close(self):
        self.events.append("close")

    async def wait_closed(self):
        self.events.append("closed")


class UploadSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = Path("/fixtures/mixed_records.xyz").read_bytes()
        self.digest = hashlib.sha256(self.source).hexdigest()
        geometries = json.loads(Path("/fixtures/expected.json").read_bytes())["geometries"]
        self.expected = [
            {
                "atoms": geometries[name]["atoms"],
                "coordinates": geometries[name]["coordinates_angstrom"],
                "coordinate_unit": "angstrom",
            }
            for name in ("water", "chloromethane")
        ]
        artifact = {"archive_sha256": "a" * 64, "member": "fixture", "size": 1, "sha256": "b" * 64}
        self.artifacts = {
            "dictionary": artifact,
            "elements": {head: {"checkpoint": artifact, "scaler": artifact} for head in ("H", "C")},
        }
        self.runtime = {"python": "3.12.12", "installed_distributions": {"scikit-learn": "1.8.0"}}
        self.worker, self.host = ScientificWorker(self), Retirement()
        self.reader, self.reader_host, self.chooser = Reader(), Retirement(), Chooser()
        self.counts = Counter()

    def assert_sources(self, geometries):
        self.assertEqual([row["source"]["record_number"] for row in geometries], [2, 1])
        self.assertEqual([row["source"]["positions"] for row in geometries], [[10, 11, 12], [3, 4, 5, 6, 7]])
        for geometry, raw in zip(geometries, self.worker.requests, strict=True):
            self.assertEqual(geometry["request_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(geometry["source"]["sha256"], self.digest)
            self.assertEqual(geometry["source"]["upload_ref"], UPLOAD_REF)
            self.assertEqual(geometry["source"]["byte_length"], len(self.source))
            self.assertEqual(geometry["preparation"]["method"], "xyz")
            self.assertEqual(geometry["preparation"]["source_unit"], "angstrom")
            self.assertEqual(geometry["preparation"]["scale_to_angstrom"], 1)

    async def allocate_reader(self):
        self.counts["reader"] += 1
        self.assertEqual(self.counts["reader"], 1)
        return self.reader, self.reader_host

    async def reply(self, request, body):
        method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ")
        path = urlsplit(target).path
        self.assertIn(b"signature:", request.lower())
        self.assertNotIn(b"bearer ", request.lower())
        if path.endswith("/uploads"):
            self.assertEqual(method, "GET")
            self.counts["membership"] += 1
            return encoded(membership(uploads=[member(byte_length=len(self.source), content_hash="sha256:" + self.digest)]))
        if path.endswith("/read-capability"):
            self.assertEqual(method, "POST")
            self.counts["grant"] += 1
            return encoded(
                grant(
                    byte_length=len(self.source),
                    content_hash="sha256:" + self.digest,
                    download_url=self.store_peer.origin + "/upload/v1/uploads/" + UPLOAD_REF + "/bytes",
                    expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                )
            )
        if path == "/provider/v1/execution-attempts/start":
            return start_response()
        if path == "/provider/v1/execution-attempts/" + ATTEMPT_REF:
            return observation()
        if path == "/provider/v1/jobs/job:example/input":
            return response(INSTRUCTIONS)
        if path.endswith("/progress"):
            return progress_response(phase=json.loads(body)["phase"])
        self.assertEqual(path, "/provider/v1/execution-attempts/complete")
        self.counts["complete"] += 1
        self.assertTrue(self.host.confirmed and self.reader_host.confirmed)
        self.assertTrue(self.reader.closed and self.reader.observed)
        self.assertTrue(all(transport.retirement_confirmed for transport in self.source_transports))
        (retained,) = await self.store.records()
        self.assertIsInstance(retained, TerminalPending)
        self.assertEqual(body, retained.body)
        content = base64.b64decode(json.loads(body)["canonical_result_base64"], validate=True)
        result = json.loads(content)
        self.result_content = content
        self.assert_sources(result["geometries"])
        self.assertEqual([item["prepared_request"] for item in result["geometries"]], self.expected)
        self.assertEqual(
            [[row["atom_index"] for row in geometry["predictions"]] for geometry in result["geometries"]], [[1, 2], [0, 2, 3, 4]]
        )
        return complete_response(
            result_schema_id=result["schema_id"],
            result_byte_length=len(content),
            result_fingerprint="sha256:" + hashlib.sha256(content).hexdigest(),
        )

    async def test_verified_upload_selected_2_1_is_retained_before_one_batch_and_published_whole(self):
        await self.run_lifecycle()

    async def run_lifecycle(self):
        headers = tuple((name, value) for name, value in store_headers(self.source) if name != b"content-length")
        async with (
            api_tls_peer("success", response_factory=self.reply) as peer,
            TlsPeer("success", response_body=self.source, response_headers=headers) as self.store_peer,
        ):
            lifecycle = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            source_api = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            source_store = HttpTransport(trust=self.store_peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            self.source_transports = (source_api, source_store)
            uploads = JobUploads(
                "job:example",
                api=api(source_api, peer.origin),
                store=source_store,
                store_origin=self.store_peer.origin,
                limits=UploadLimits(len(self.source), len(self.source), 4, 10),
            )
            preparation = InputPreparation(
                uploads=uploads,
                source_api_transport=source_api,
                source_store_transport=source_store,
                allocate_reader=self.allocate_reader,
                chooser=self.chooser,
                max_worker_calls=2,
                allocate_generator=AsyncMock(side_effect=AssertionError("Upload fixture unexpectedly allocated a generator.")),
                generation_settings=GenerationSettings(17, 200, 1),
                generation_seconds=2,
                seconds=10,
            )
            session = NMRNetSession(
                worker=self.worker,
                retirement=self.host,
                execution_identity={"image_id": "fixture"},
                selected_artifacts=self.artifacts,
                runtime_expectations=self.runtime,
                input_preparation=preparation,
            )
            recovery_scope = replace(scope(), api_origin=peer.origin)
            with tempfile.TemporaryDirectory() as directory:
                self.store = AttemptStore(
                    Path(directory), scope=recovery_scope, limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=1
                )
                await self.store.open()
                try:
                    controller = AttemptController(
                        api(lifecycle, peer.origin),
                        self.store,
                        AttemptClaims(),
                        session,
                        allowed_job_states=frozenset({"open"}),
                        poll_seconds=1,
                        task_settle_seconds=0.1,
                    )
                    confirmed = await controller.run(replace(record(), scope=recovery_scope, input=facts(INSTRUCTIONS)))
                    self.assertIsInstance(confirmed, TerminalConfirmed)
                    self.assertEqual(await self.store.records(), (confirmed,))
                finally:
                    await session.retire()
                    await self.store.close()
                    for transport in (lifecycle, *self.source_transports):
                        await transport.retire()
                reopened = AttemptStore(
                    Path(directory), scope=recovery_scope, limits=StoreLimits(1, MAX_RECORD_BYTES, 0, confirmed_history_records=64), operation_seconds=1
                )
                await reopened.open()
                try:
                    self.assertEqual(await reopened.records(), (confirmed,))
                finally:
                    await reopened.close()
            self.assertEqual(self.worker.events, ["prepare", "execute", "close", "closed"])
            self.assertEqual(self.chooser.calls, 1)
            self.assertEqual(self.counts, Counter(membership=2, grant=1, reader=1, complete=1))
            self.assertEqual(len(self.store_peer.requests), 1)
            self.assertIn(b"authorization: bearer ", self.store_peer.requests[0].lower())
            self.assertNotIn(b"signature:", self.store_peer.requests[0].lower())
