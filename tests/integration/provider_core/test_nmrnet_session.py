"""Installed core publishes a NMRNet result through the real signed TLS/store path.

Science and host retirement are recording ports here; their real-worker and
real-engine qualifications remain separate and are not claimed by this fixture.
"""

import asyncio
import base64
import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import complete_response, observation, progress_response, start_response
from tests.fakes.job_input import facts, response
from tests.fakes.publication import api
from tests.fakes.tls_peer import TlsPeer

from nmrnet_provider.input_geometry import InputRejected, prepare_json
from nmrnet_provider.result import MAX_RESULT_BYTES, ResultPlan
from nmrnet_provider.session import NMRNetSession, PreparationEvidenceFailed, ResultAssemblyFailed, SessionRetirementFailed
from nmrnet_provider.submission import PreparedJobInput
from provider_core.analysis import AnalysisFailure, PreparedAnalysis
from provider_core.attempt_record import MAX_EVIDENCE_BYTES, MAX_RECORD_BYTES, TerminalConfirmed, acknowledge_start
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.controller import AttemptController
from provider_core.job_input import verify_job_input
from provider_core.ownership import AttemptClaims
from provider_core.transport import HttpTransport

RAW = b'{"atoms":["H"],"coordinates":[[0,0,0]],"coordinate_unit":"angstrom"}'


class ScientificWorker:
    def __init__(self, artifacts, runtime):
        self.artifacts, self.runtime = artifacts, runtime
        self.events = []
        self.handle = object()

    async def prepare(self, raw_requests):
        self.events.append("prepare")
        (self.raw,) = raw_requests
        return self.handle

    async def execute(self, handle):
        if handle is not self.handle:
            raise AssertionError("Wrong worker handle")
        self.events.append("execute")
        return (
            {
                "request_sha256": hashlib.sha256(self.raw).hexdigest(),
                "prediction": {"unit": "ppm", "predictions": [{"atom_index": 0, "element": "H", "shift_ppm": 1.2}]},
                "provenance": {"artifacts": copy.deepcopy(self.artifacts), "runtime": copy.deepcopy(self.runtime)},
                "private_warnings": [],
            },
        )

    def close(self):
        self.events.append("close")

    async def wait_closed(self):
        self.events.append("closed")


class HostOwner:
    def __init__(self):
        self.confirmed = False
        self.failure = None

    async def retire(self):
        if self.failure is not None:
            raise self.failure
        self.confirmed = True


class NMRNetSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        member = {"archive_sha256": "a" * 64, "member": "fixture", "size": 1, "sha256": "b" * 64}
        artifacts = {"dictionary": member, "elements": {"H": {"checkpoint": member, "scaler": member}}}
        runtime = {"python": "3.12.12", "installed_distributions": {"scikit-learn": "1.8.0"}}
        self.worker = ScientificWorker(artifacts, runtime)
        self.host = HostOwner()
        self.session = self.make_session()
        self.pending = replace(record(), input=facts(RAW))
        self.acknowledged = acknowledge_start(self.pending, admission())
        self.instructions = verify_job_input(response(RAW), feed=facts(RAW))

    def make_session(self, preparation=None):
        return NMRNetSession(
            worker=self.worker,
            retirement=self.host,
            execution_identity={"image_id": "fixture"},
            selected_artifacts=self.worker.artifacts,
            runtime_expectations=self.worker.runtime,
            input_preparation=preparation,
        )

    async def test_preparation_owner_supplies_one_ordered_batch_and_retires_with_session(self):
        first = prepare_json(RAW)
        second = prepare_json(b'{"atoms":["H"],"coordinates":[[1,2,3]],"coordinate_unit":"angstrom"}')
        preparation = AsyncMock()
        preparation.prepare.return_value = PreparedJobInput((second, first), "xyz")
        self.session = self.make_session(preparation)
        handle = object()
        with patch.object(self.worker, "prepare", new=AsyncMock(return_value=handle)) as dispatch:
            candidate = await self.session.prepare(self.acknowledged, self.instructions)
        preparation.prepare.assert_awaited_once_with(self.instructions)
        dispatch.assert_awaited_once_with((second.request_bytes, first.request_bytes))
        retained = json.loads(candidate.identity.content)["geometries"]
        self.assertEqual(
            [hashlib.sha256(item.request_bytes).hexdigest() for item in (second, first)],
            [item["request_sha256"] for item in retained],
        )
        await self.session.retire()
        preparation.retire.assert_awaited_once_with()
        self.assertTrue(self.host.confirmed)

    async def test_preparation_cleanup_failure_does_not_skip_scientific_retirement(self):
        failure = OSError("private source cleanup evidence")
        preparation = AsyncMock()
        preparation.retire.side_effect = failure
        self.session = self.make_session(preparation)
        with self.assertRaises(SessionRetirementFailed) as caught:
            await self.session.retire()
        self.assertEqual(caught.exception.failures, (("input_retirement", failure),))
        self.assertTrue(self.host.confirmed)
        self.assertEqual(self.worker.events, ["close", "closed"])

    async def test_retirement_during_input_preparation_prevents_scientific_dispatch(self):
        await self.assert_late_input_blocked(rejected=False)

    async def test_input_rejection_after_retirement_cannot_become_a_job_outcome(self):
        await self.assert_late_input_blocked(rejected=True)

    async def assert_late_input_blocked(self, *, rejected):
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(_instructions):
            entered.set()
            await release.wait()
            if rejected:
                raise InputRejected("Cannot read selected record: its atom count is missing.")
            return PreparedJobInput((prepare_json(RAW),), "xyz")

        preparation = AsyncMock()
        preparation.prepare.side_effect = prepare
        self.session = self.make_session(preparation)
        task = asyncio.create_task(self.session.prepare(self.acknowledged, self.instructions))
        await entered.wait()
        await self.session.retire()
        release.set()
        with self.assertRaisesRegex(ValueError, "retirement"):
            await task
        self.assertEqual(self.worker.events, ["close", "closed"])

    async def test_identity_capacity_stops_dispatch_even_when_the_whole_result_fits(self):
        """Inject a planned batch at the binding seam; this does not qualify batch execution."""
        raw = json.dumps({"coordinate_unit": "angstrom", "atoms": ["O"] * 128, "coordinates": [[0, 0, 0]] * 128}).encode()
        geometry = replace(
            prepare_json(raw), source_position_kind="atom_id", source_positions=tuple(2**53 - 1 - index for index in range(128))
        )
        geometries = tuple(replace(geometry, source_record_number=index) for index in range(1, 113))
        plan = ResultPlan(
            geometries,
            preparation_method="mol2",
            job_ref=self.instructions.job_ref,
            execution_attempt_ref=self.acknowledged.admission.execution_attempt_ref,
            input_fingerprint=self.instructions.identity.input_fingerprint,
            execution_identity={"image_id": "fixture"},
            selected_artifacts=self.worker.artifacts,
            runtime_expectations=self.worker.runtime,
        )
        self.assertGreater(len(plan.prepared_identity), MAX_EVIDENCE_BYTES)
        self.assertLessEqual(plan.maximum_result_bytes, MAX_RESULT_BYTES)
        with patch("nmrnet_provider.session.ResultPlan", return_value=plan), self.assertRaisesRegex(ValueError, "evidence"):
            await self.session.prepare(self.acknowledged, self.instructions)
        self.assertEqual([], self.worker.events)
        await self.session.retire()
        self.assertTrue(self.host.confirmed)

    async def test_exact_handle_is_consumed_and_retirement_requires_host(self):
        handle = await self.session.prepare(self.acknowledged, self.instructions)
        self.assertIs(type(handle), PreparedAnalysis)
        self.assertEqual("nmrnet.prepared_input.v1", handle.identity.schema_id)
        candidate = json.loads(handle.identity.content)
        self.assertEqual(hashlib.sha256(self.worker.raw).hexdigest(), candidate["geometries"][0]["request_sha256"])
        self.assertNotIn("prepared_request", candidate["geometries"][0])
        with self.assertRaises(ValueError):
            await self.session.execute(object())
        with self.assertRaises(ValueError):
            await self.session.execute(PreparedAnalysis(handle.identity))
        outcome = await self.session.execute(handle)
        self.assertEqual(json.loads(outcome.content)["geometries"][0]["prepared_request"], json.loads(RAW))
        with self.assertRaises(ValueError):
            await self.session.execute(handle)
        self.host.failure = RuntimeError("private host evidence")
        with self.assertRaises(SessionRetirementFailed):
            await self.session.retire()
        self.assertFalse(self.host.confirmed)
        self.host.failure = None
        await self.session.retire()
        self.assertTrue(self.host.confirmed)
        self.assertEqual(self.worker.events.count("execute"), 1)

    async def test_bad_input_rejects_before_worker_and_cannot_be_retried(self):
        raw = b'{"atoms":'
        acknowledged = acknowledge_start(replace(record(), input=facts(raw)), admission())
        outcome = await self.session.prepare(acknowledged, verify_job_input(response(raw), feed=facts(raw)))
        self.assertIsInstance(outcome, AnalysisFailure)
        self.assertEqual(self.worker.events, [])
        with self.assertRaises(ValueError):
            await self.session.prepare(self.acknowledged, self.instructions)
        await self.session.retire()
        self.assertTrue(self.host.confirmed)

    async def test_reader_diagnostic_is_retained_privately_with_expected_input_failure(self):
        preparation = AsyncMock()
        preparation.prepare.side_effect = InputRejected(
            "Cannot read selected record: missing atoms.", diagnostic="private native canary"
        )
        session = self.make_session(preparation)
        outcome = await session.prepare(self.acknowledged, self.instructions)
        self.assertIsInstance(outcome, AnalysisFailure)
        self.assertNotIn("private native", outcome.message)
        self.assertEqual(json.loads(outcome.evidence.content)["diagnostic"], "private native canary")
        self.assertEqual(self.worker.events, [])
        await session.retire()

    async def test_oversized_reader_diagnostic_is_kept_whole_when_evidence_admission_fails(self):
        rejection = InputRejected("Cannot read selected record: missing atoms.", diagnostic="private" * 40000)
        preparation = AsyncMock()
        preparation.prepare.side_effect = rejection
        session = self.make_session(preparation)
        with self.assertRaises(PreparationEvidenceFailed) as caught:
            await session.prepare(self.acknowledged, self.instructions)
        self.assertIs(caught.exception.rejection, rejection)
        self.assertEqual(len(caught.exception.rejection.diagnostic), 280000)
        self.assertIsNotNone(caught.exception.__cause__)
        self.assertEqual(self.worker.events, [])
        await session.retire()

    async def test_substituted_verified_input_never_reaches_worker(self):
        with self.assertRaises(ValueError):
            await self.session.prepare(self.acknowledged, verify_job_input(response(b"different"), feed=facts(b"different")))
        self.assertEqual(self.worker.events, [])
        await self.session.retire()

    async def test_failed_assembly_preserves_received_outcome_without_rerun(self):
        handle = await self.session.prepare(self.acknowledged, self.instructions)
        result = await self.worker.execute(self.worker.handle)
        result[0]["prediction"]["unit"] = "wrong"
        with patch.object(self.worker, "execute", new=AsyncMock(return_value=result)) as execute:
            with self.assertRaises(ResultAssemblyFailed) as failure:
                await self.session.execute(handle)
            self.assertIs(failure.exception.worker_result, result)
            self.assertIsNotNone(failure.exception.__cause__)
            with self.assertRaises(ValueError):
                await self.session.execute(handle)
            self.assertEqual(execute.await_count, 1)
        await self.session.retire()

    async def test_evidence_overflow_preserves_untruncated_worker_outcome(self):
        handle = await self.session.prepare(self.acknowledged, self.instructions)
        result = await self.worker.execute(self.worker.handle)
        result[0]["private_warnings"] = [
            {
                "category": "builtins.UserWarning",
                "head": "H",
                "versions": None,
                "message": "private" * 40000,
                "filename": "/private",
                "lineno": 1,
            }
        ]
        with (
            patch.object(self.worker, "execute", new=AsyncMock(return_value=result)),
            self.assertRaises(ResultAssemblyFailed) as failure,
        ):
            await self.session.execute(handle)
        self.assertIs(failure.exception.worker_result, result)
        self.assertEqual(len(result[0]["private_warnings"][0]["message"]), 280000)
        await self.session.retire()

    async def test_cancelled_preparation_cannot_be_reused(self):
        entered = asyncio.Event()

        async def blocked(_raw):
            entered.set()
            await asyncio.Future()

        with patch.object(self.worker, "prepare", side_effect=blocked):
            task = asyncio.create_task(self.session.prepare(self.acknowledged, self.instructions))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        await self.session.retire()
        self.assertTrue(self.host.confirmed)
        with self.assertRaises(ValueError):
            await self.session.prepare(self.acknowledged, self.instructions)

    async def test_local_close_failure_does_not_skip_host_cleanup(self):
        with (
            patch.object(self.worker, "close", side_effect=OSError("private local failure")),
            self.assertRaises(SessionRetirementFailed),
        ):
            await self.session.retire()
        self.assertTrue(self.host.confirmed)
        await self.session.retire()

    async def test_cleanup_preserves_primary_and_all_later_failures(self):
        first = OSError("private close failure")
        host = RuntimeError("private host failure")
        last = OSError("private wait failure")
        self.host.failure = host
        with (
            patch.object(self.worker, "close", side_effect=first),
            patch.object(self.worker, "wait_closed", new=AsyncMock(side_effect=last)) as wait,
            self.assertRaises(SessionRetirementFailed) as failure,
        ):
            await self.session.retire()
        self.assertIs(failure.exception.cause, first)
        self.assertEqual(
            failure.exception.failures, (("channel_close", first), ("host_retirement", host), ("channel_observation", last))
        )
        for cause in (first, host, last):
            self.assertNotIn(str(cause), str(failure.exception))
        wait.assert_awaited_once()

    async def test_cancelled_host_retirement_keeps_prior_failure_and_observes_local_close(self):
        first = OSError("private close failure")
        cancelled = asyncio.CancelledError()
        self.host.failure = cancelled
        with patch.object(self.worker, "close", side_effect=first), self.assertRaises(asyncio.CancelledError) as failure:
            await self.session.retire()
        self.assertIs(failure.exception, cancelled)
        self.assertIs(self.session.last_retirement_failure.cause, first)
        self.assertEqual(self.worker.events, ["closed"])

    async def test_signed_api_lifecycle_publishes_exact_retained_scientific_result(self):
        replies = [
            start_response(),
            observation(),
            response(RAW),
            progress_response(),
            observation(),
            progress_response(phase="running"),
            observation(),
            observation(),
        ]

        async def respond(request, body):
            target = request.split(b"\r\n", 1)[0].split()[1]
            if target.endswith(b"/complete"):
                self.assertTrue(self.host.confirmed)
                self.assertEqual(self.worker.events, ["prepare", "execute", "close", "closed"])
                content = base64.b64decode(json.loads(body)["canonical_result_base64"], validate=True)
                return complete_response(
                    result_schema_id="nmrnet.coordinate_shifts.result.v1",
                    result_fingerprint="sha256:" + hashlib.sha256(content).hexdigest(),
                    result_byte_length=len(content),
                )
            return replies.pop(0)

        async with TlsPeer("success", response_factory=respond) as peer:
            recovery_scope = replace(scope(), api_origin=peer.origin)
            pending = replace(self.pending, scope=recovery_scope)
            transport = HttpTransport(trust=peer.trust, exchange_seconds=1, retirement_seconds=0.5)
            client = api(transport, peer.origin)
            with tempfile.TemporaryDirectory() as directory:
                store = AttemptStore(
                    Path(directory), scope=recovery_scope, limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1
                )
                await store.open()
                try:
                    controller = AttemptController(
                        client,
                        store,
                        AttemptClaims(),
                        self.session,
                        allowed_job_states=frozenset({"open"}),
                        poll_seconds=1,
                        task_settle_seconds=0.1,
                    )
                    confirmed = await controller.run(pending)
                    terminal = confirmed.pending
                    self.assertIsInstance(confirmed, TerminalConfirmed)
                    self.assertEqual(await store.records(), (confirmed,))
                    self.assertEqual(peer.request_bodies[-1], terminal.body)
                    candidate = json.loads(terminal.attempt.prepared_identity.content)
                    result = json.loads(base64.b64decode(json.loads(terminal.body)["canonical_result_base64"]))
                    self.assertEqual(candidate["geometries"][0]["request_sha256"], result["geometries"][0]["request_sha256"])
                    self.assertEqual(self.worker.events, ["prepare", "execute", "close", "closed"])
                    self.assertEqual(len(peer.requests), 9)
                finally:
                    await store.close()
            await transport.retire()
