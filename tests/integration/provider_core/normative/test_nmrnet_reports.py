"""Owned diagnostics reach the single stderr logging sink with causal context.

Foreground process tests separately exercise the actual nonblocking descriptor.
"""

import asyncio
import errno
import hashlib
import io
import itertools
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import httpcore
from contextlib import closing, redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.fakes.acquisition import file_sources, private_source_directory, snapshot_source, store_headers
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import admission, record
from tests.fakes.attempts import ATTEMPT_REF, observation, progress_response
from tests.fakes.condition_recovery import ConditionOutageTransport
from tests.fakes.conditions import ConditionTransport, ReportingSession, SwallowingSession
from tests.fakes.controller import empty_store
from tests.fakes.execution import Session, acknowledged_store, unexpected_condition
from tests.fakes.checkpoint_execution import RecordingDelivery
from tests.fakes.job_input import response
from tests.fakes.api import api
from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.interpreter_ports import chat_endpoint
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import PreparationRetirementFailed
from nmrnet_provider.interpreter import ModelReportedInputProblem, _InputProblemReport
from nmrnet_provider.native_factory import NativeAttemptFactory, NativePolicy, NativeSetupFailed
from nmrnet_provider.process import _failure_report
from nmrnet_provider.process_reports import ProviderProcessFailed, failure_evidence
from nmrnet_provider.result import ResultRejected
from nmrnet_provider.session import PredictionRecoveryFailed, PreparationEvidenceFailed, ResultAssemblyFailed
from nmrnet_provider.smiles import ConformerShortfall, GeneratedConformer, GeneratedConformers
from nmrnet_provider.smiles_protocol import ConformerOutputLimit
from nmrnet_provider.worker_client import PredictionExecutionFailed
from provider_core.api import ApiFailure
from provider_core.attempt_record import MAX_EVIDENCE_BYTES, TerminalOperation, acknowledge_start
from provider_core.attempt_request import fail_body
from provider_core.attempt_response import AttemptObservation
from provider_core.checkpoint_store import CheckpointFailed, CheckpointRefused
from provider_core.chat import ChatEndpoint, ChatEndpointSpec, ChatFailed
from provider_core.controller import LiveStartPolicy
from provider_core.conversation import ConversationFailed, ToolConversation, ToolRejected
from provider_core.credential import CredentialRejected
from provider_core.execution import (
    AttemptExecutor,
    ConditionRecovery,
    ConditionRecoveryExpired,
    ExecutionFailed,
    ExecutionRefused,
    ExecutionStopped,
    ObservationRecovery,
)
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.delivery_client import DeliveryRejected
from provider_core.delivery_store import DeliveryOutcome, DeliveryStorageFailed
from provider_core.delivery_worker import DeliveryWorkerFailed
from provider_core.producer_checkpoint import AcknowledgedCheckpoint, EnteredCheckpoint, TerminalSelectedCheckpoint
from provider_core.recovery import ObservedAttempt, RecoveryFailed, RecoveryRefused
from provider_core.starting import StartFailed, StartRefused
from provider_core.transport import ExchangeFailed, HttpFileResponse, HttpResponse, HttpResponseHead
from provider_core.uploads import UploadFailed, UploadFileReadFailed, UploadInputRejected, read_upload_file


class ReceivingReportTests(unittest.IsolatedAsyncioTestCase):
    def test_sqlite_native_identity_reaches_operator_stderr_without_sql_or_private_names(self):
        with closing(sqlite3.connect(":memory:")) as database:
            try:
                database.execute('SELECT * FROM "PRIVATE_SQL_TABLE"')
            except sqlite3.Error as cause:
                report = self.receive(CheckpointFailed("Cannot retain producer control", cause=cause))
        rendered = json.dumps(report)
        self.assertIn('"sqlite_errorcode": 1', rendered)
        self.assertIn('"sqlite_errorname": "SQLITE_ERROR"', rendered)
        self.assertNotIn("PRIVATE_SQL_TABLE", rendered)
        self.assertNotIn("SELECT", rendered)

    async def test_connect_rejection_reaches_operator_as_proxy_status_without_private_reason_or_api_status(self):
        report = self.receive(ExchangeFailed(
            "The CONNECT proxy refused the tunnel",
            primary=httpcore.ProxyError("PRIVATE_PROXY_REASON"), proxy_connect_status=502,
        ))
        rendered = json.dumps(report)
        self.assertIn('"proxy_connect_status": 502', rendered)
        self.assertNotIn('"status": 502', rendered)
        self.assertNotIn("PRIVATE_PROXY_REASON", rendered)

    async def test_model_rejection_reason_reaches_protected_renderer_after_all_key_redaction(self):
        key, other_key = "CURRENT_MODEL_KEY", "FALLBACK_CREDENTIAL_" + "z" * 80
        prompt = 'PRIVATE_INPUT "coordinates"\nC 0 0 0; api_key=' + key
        reason = "Unsupported tool_choice; password=PRIVATE_FIELD; " + "x" * 940 + other_key
        response = HttpResponse(400, ((b"x-request-id", b"request:model-rejection"),), json.dumps({"error": {
            "message": reason, "code": "unsupported_parameter", "param": "tool_choice",
            "request": "PRIVATE_RESPONSE_DUMP",
        }}).encode())
        transport = SimpleNamespace(exchange=AsyncMock(return_value=response), retire=AsyncMock(), retirement_confirmed=True)
        endpoint = ChatEndpoint(
            ChatEndpointSpec(configuration_id="fixture", model="fixture", url="https://example.invalid/chat",
                             api_key=key, reasoning_effort=None),
            transport=transport, request_bytes=4096, response_bytes=16384,
        )
        with self.assertRaises(ChatFailed) as caught:
            await endpoint.complete([{"role": "user", "content": prompt}], [])
        rendered = json.dumps(self.receive(caught.exception, secrets=(key, other_key)))
        for useful in ("Unsupported tool_choice", "unsupported_parameter", "request:model-rejection",
                       '"endpoint_error_unverified": true', '"status": 400'):
            self.assertIn(useful, rendered)
        for private in (key, "FALLBACK_CREDENTIAL_", "PRIVATE_FIELD", "PRIVATE_RESPONSE_DUMP", "PRIVATE_INPUT", "C 0 0 0"):
            self.assertNotIn(private, rendered)

    def test_checkpoint_failure_preserves_private_io_cause_without_inventing_history_facts(self):
        failure = CheckpointFailed("Producer checkpoint operation failed", cause=OSError(errno.EIO, "owned key=credential-canary"))
        rendered = json.dumps(self.receive(failure, secrets=("credential-canary",)))
        self.assertIn("Producer checkpoint operation failed", rendered)
        self.assertIn('"errno": 5', rendered)
        self.assertNotIn("credential-canary", rendered)
        self.assertNotIn("history_", rendered)
        self.assertNotIn("record_visible", rendered)

    def test_prediction_recovery_keeps_first_diagnostic_and_independent_cleanup_in_stderr(self):
        first = PredictionExecutionFailed("Allocator failed; api_key=PRIVATE_CREDENTIAL; known-key")
        failure = PredictionRecoveryFailed(first, OSError(errno.EIO, "PRIVATE_PATH"))
        report = self.receive(failure, secrets=("known-key",))
        rendered = json.dumps(report)
        self.assertIn("first prediction failure", rendered)
        self.assertIn("Allocator failed", rendered)
        self.assertIn('"errno": 5', rendered)
        self.assertNotIn("PRIVATE", rendered)
        self.assertNotIn("known-key", rendered)

    async def test_condition_expiry_retains_first_latest_and_uncertainty_in_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = ConditionOutageTransport(first_failed_put=5)
            transport.faults = itertools.chain(
                (reply(b"PRIVATE first body", status=503, headers=((b"x-request-id", b"request:condition-first"),)),),
                itertools.repeat(
                    reply(b"PRIVATE latest body", status=503, headers=((b"x-request-id", b"request:condition-latest"),))
                ),
            )
            session = SwallowingSession(store)
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(),
                on_delivery_retry=Mock(),
                delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.04,
                allowed_job_states=frozenset({"open"}),
                poll_seconds=0.005,
                task_settle_seconds=0.1,
                condition_recovery=ConditionRecovery(0.05, 0.01, Mock(), Mock()),
            )
            try:
                with self.assertRaises(ExecutionFailed) as caught:
                    await asyncio.wait_for(executor.run(acknowledged), 2)
                self.assertIs(type(caught.exception.cause), ConditionRecoveryExpired)
                self.assertIs(caught.exception.outcome, session.outcome)
                report = self.receive(caught.exception)
                rendered = json.dumps(report)
                for fact in (
                    "ConditionRecoveryExpired",
                    "request:condition-first",
                    "request:condition-latest",
                    '"condition_code": "waiting_capacity"',
                    '"phase": "running"',
                    '"condition_receipt_received": false',
                    '"attempt_mutation_may_have_committed": true',
                ):
                    self.assertIn(fact, rendered)
                self.assertNotIn("PRIVATE", rendered)
                with self.assertRaises(ExecutionFailed):
                    await executor.settle()
                await executor.retain_suppressed_disposition()
                claim.release()
            finally:
                await store.close()

    def test_start_failure_keeps_uncertain_send_without_inventing_attempt(self):
        pending = record()
        outage = ApiFailure(
            "start Execution Attempt",
            "API returned HTTP 503; no verified response was obtained",
            response=HttpResponse(503, ((b"x-request-id", b"request:start-outage"),), b"PRIVATE_SERVER_BODY"),
        )
        outage.attempt_mutation_may_have_committed = True
        start_failure = StartFailed(
            "Attempt start lacks a durable owned acknowledgement",
            cause=outage,
            acknowledgement=None,
            api_failure=outage,
            settlement_failures=(),
        )
        report = self.receive(start_failure, secrets=(pending.input.job_ref,))
        rendered = json.dumps(report)
        for fact in (
            "Attempt start lacks a durable owned acknowledgement",
            "request:start-outage",
            '"status": 503',
            '"attempt_mutation_may_have_committed": true',
        ):
            self.assertIn(fact, rendered)
        self.assertNotIn("execution_attempt_ref", rendered)
        self.assertNotIn("PRIVATE_SERVER_BODY", rendered)
        self.assertNotIn(pending.input.job_ref, rendered)

    async def test_native_allocation_failure_identifies_selected_job_at_factory_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            transport = Transport()
            factory = NativeAttemptFactory(
                config=SimpleNamespace(),
                claims=AttemptClaims.live(),
                execution_identity={},
                selected_artifacts={},
                policy=NativePolicy(allocation_seconds=1, prediction_seconds=1, source_seconds=1),
                observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
                condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
                on_prediction_retry=Mock(),
                on_operator_notice=Mock(),
                on_unconfirmed_phase_progress=Mock(),
                on_delivery_retry=Mock(),
                start_policy=LiveStartPolicy(1, Mock(), Mock()),
            )
            allocation = Mock(
                start=AsyncMock(side_effect=PermissionError(errno.EACCES, "PRIVATE_PATH")), retire=AsyncMock(), settle=AsyncMock()
            )
            allocation.retirement = allocation
            try:
                with (
                    patch("nmrnet_provider.native_factory.NativeOperation", return_value=allocation),
                    self.assertRaises(NativeSetupFailed) as caught,
                ):
                    await factory.allocate_prediction(job_ref="job:example")
                report = self.receive(caught.exception)
                rendered = json.dumps(report)
                self.assertIn('"job_ref": "job:example"', rendered)
                self.assertIn('"errno": 13', rendered)
                self.assertNotIn("execution_attempt_ref", rendered)
                self.assertNotIn("PRIVATE", rendered)
                self.assertEqual(await store.inventory(), ())
                self.assertEqual(transport.calls, [])
                self.assertEqual(factory.allocations, [])
                self.assertIsNone(factory.last_failure)
                allocation.retire.assert_awaited_once()
                with (
                    patch("nmrnet_provider.native_factory.NativeOperation", return_value=allocation),
                    self.assertRaises(NativeSetupFailed) as source,
                ):
                    await factory.allocate_reader()
                report = self.receive(source.exception)
                self.assertNotIn("job_ref", json.dumps(report))
                self.assertNotIn("execution_attempt_ref", json.dumps(report))
                self.assertEqual(caught.exception.job_ref, "job:example")
            finally:
                await store.close()

    def test_read_only_recovery_diagnostics_distinguish_observation_from_receipt_and_durability(self):
        acknowledged = AcknowledgedCheckpoint(record(), admission())
        selected = TerminalSelectedCheckpoint(acknowledged, TerminalOperation.FAIL, "sha256:" + "b" * 64, 42)
        observed = ObservedAttempt(selected, AttemptObservation(ATTEMPT_REF, "job:example", "succeeded", "closed"))
        send = ApiFailure(
            "publish outcome", "No verified response was obtained",
            response=HttpResponse(409, ((b"x-request-id", b"request:publication"),
                                        (b"authorization", b"PRIVATE_HEADER")), b"PRIVATE_BODY"),
        )
        publication = DeliveryWorkerFailed(cause=send, api_failure=send, cleanup_failures=(), api_receipt_received=False)
        rendered = json.dumps(self.receive(publication))
        self.assertIn('"api_receipt_received": false', rendered)
        self.assertIn("request:publication", rendered)
        self.assertNotIn("PRIVATE", rendered)
        for result in (None, observed):
            failure = RecoveryFailed(OSError(errno.EIO, "PRIVATE_PATH"), observed_record=result)
            report = self.receive(failure)
            owner = report["relationships"][0]["failure"]
            self.assertIs(owner["attempt_observation_received"], result is observed)
            rendered = json.dumps(report)
            self.assertIn('"errno": 5', rendered)
            if result is observed:
                self.assertIn('"observed_attempt_state": "succeeded"', rendered)
                self.assertIn('"observed_job_state": "closed"', rendered)
            self.assertNotIn("PRIVATE", rendered)
            self.assertNotIn("api_receipt_received", rendered)
            self.assertNotIn("readback_retained", rendered)

    # silent-defaults: allow-contract -- Fixtures without configured credentials still exercise the same receiving renderer; credential cases supply explicit keys.
    def receive(self, failure, *, secrets=()):
        output = io.StringIO()
        with (
            redirect_stderr(output),
            patch("nmrnet_provider.process._write_stderr", side_effect=lambda raw: sys.stderr.write(raw.decode("ascii"))),
        ):
            _failure_report((("operation", failure),), secrets=secrets, held=False)
        return json.loads(output.getvalue())

    async def test_returned_outcome_in_memory_does_not_claim_durable_retention_or_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            session = Session(store)
            session.outcome = replace(session.outcome, content=b"PRIVATE_RESULT_BYTES")
            transport = Transport(
                reply(observation()),
                reply(response()),
                reply(progress_response()),
                reply(observation()),
                reply(progress_response(phase="running")),
                reply(observation()),
                reply(observation(job_state="closed")),
            )
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(),
                on_delivery_retry=Mock(),
                delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.04,
                allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
            )
            try:
                with self.assertRaises(ExecutionFailed) as caught:
                    await executor.run(acknowledged)
                self.assertIs(caught.exception.outcome, session.outcome)
                (retained,) = await store.inventory()
                self.assertIs(type(retained), EnteredCheckpoint)
                self.assertEqual(len(transport.calls), 7)
                report = self.receive(caught.exception)
                rendered = json.dumps(report)
                self.assertIn('"outcome_in_memory": true', rendered)
                self.assertIn('"unconfirmed_record_in_memory": false', rendered)
                self.assertNotIn("outcome_retained", rendered)
                self.assertNotIn("unconfirmed_record_retained", rendered)
                self.assertNotIn("PRIVATE", rendered)
                with self.assertRaises(ExecutionFailed):
                    await executor.settle()
                await executor.retain_suppressed_disposition()
                claim.release()
            finally:
                await store.close()

    async def test_failed_condition_and_later_binding_error_keep_both_receiving_causes(self):
        class LaterFailureSession(ReportingSession):
            async def prepare(self, _acknowledged, _instructions, *, report_condition):
                try:
                    await report_condition("waiting_capacity")
                except Exception as cause:
                    self.report_failure = cause
                raise ResultRejected("Cannot assemble authored result PRIVATE_BINDING_SECRET")

        with tempfile.TemporaryDirectory() as directory:
            store, acknowledged, claim = await acknowledged_store(directory)
            transport = ConditionTransport()
            transport.release.set()
            transport.failure = ExchangeFailed(
                "Cannot receive authored progress acknowledgement", primary=OSError(errno.ETIMEDOUT, "PRIVATE_TRANSPORT")
            )
            session = LaterFailureSession(store)
            session.retirement_failure = OSError(errno.EIO, "PRIVATE_CLEANUP")
            executor = AttemptExecutor(
                api(transport, store.scope.api_origin),
                store,
                claim,
                session,
                delivery=RecordingDelivery(),
                on_delivery_retry=Mock(),
                delivery_retry_initial_seconds=0.01,
                delivery_retry_max_seconds=0.04,
                allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
            )
            try:
                with self.assertRaises(ExecutionFailed) as caught:
                    await executor.run(acknowledged)
                self.assertIs(caught.exception.cause, session.report_failure)
                report = self.receive(caught.exception, secrets=("PRIVATE_BINDING_SECRET",))
                rendered = json.dumps(report)
                for fact in (
                    "ApiFailure",
                    "ResultRejected",
                    "Cannot assemble authored result",
                    "ExchangeFailed",
                    '"errno": 110',
                    '"errno": 5',
                    '"relationship": "context"',
                    "independent cleanup",
                ):
                    self.assertIn(fact, rendered)
                self.assertIn('"outcome_in_memory": false', rendered)
                self.assertIn('"unconfirmed_record_in_memory": false', rendered)
                self.assertNotIn("outcome_retained", rendered)
                self.assertNotIn("unconfirmed_record_retained", rendered)
                self.assertNotIn("PRIVATE", rendered)
                session.retirement_failure = None
                await executor.settle()
                claim.release()
            finally:
                await store.close()

    async def test_session_cleanup_cancellation_keeps_its_nested_owned_reason_for_the_operator(self):
        ports = SessionPorts()
        report = ModelReportedInputProblem(
            _InputProblemReport("other_input_problem", "model could not identify the source unit"), b"{}"
        )
        previous = PreparationRetirementFailed(
            (("model retirement", OSError(errno.EIO, "PRIVATE_CLEANUP")),), operation_failure=report
        )
        interruption = asyncio.CancelledError()
        interruption.__cause__ = previous
        with (
            patch.object(ports.preparation, "retire", side_effect=interruption),
            self.assertRaises(asyncio.CancelledError) as caught,
        ):
            await ports.session.retire()
        self.assertIs(caught.exception, interruption)
        self.assertIs(ports.session.last_retirement_failure.interruption_cause, previous)
        received = self.receive(caught.exception)
        rendered = json.dumps(received)
        self.assertIn("model could not identify the source unit", rendered)
        self.assertIn('"independently_verified": false', rendered)
        self.assertIn('"errno": 5', rendered)
        self.assertNotIn("PRIVATE", rendered)
        self.assertEqual(ports.worker.events, [])
        self.assertFalse(ports.host.confirmed)

    async def test_exhausted_real_endpoints_report_unsent_and_http_failures_without_payloads(self):
        async with (
            TlsPeer("success") as first,
            TlsPeer(
                "success",
                response_status=503,
                response_body=b"PRIVATE_MODEL_BODY",
                response_headers=((b"x-request-id", b"request:second-endpoint"), (b"x-private", b"PRIVATE_HEADER")),
            ) as second,
        ):
            conversation = ToolConversation(
                (chat_endpoint(first, identifier="first", request_bytes=1), chat_endpoint(second, identifier="second")),
                max_turns=2,
                secrets=("PRIVATE_MODEL_KEY",),
            )
            dispatch = AsyncMock()
            with self.assertRaises(ConversationFailed) as caught:
                await conversation.run([{"role": "user", "content": "PRIVATE_JOB"}], [], dispatch)
            await conversation.retire()
            report = self.receive(caught.exception, secrets=("PRIVATE_MODEL_KEY",))
            rendered = json.dumps(report)
            for fact in (
                "interpreter endpoint 0 failure",
                "interpreter endpoint 1 failure",
                '"reason": "request_limit"',
                '"reason": "http_status"',
                '"status": 503',
                "request:second-endpoint",
            ):
                self.assertIn(fact, rendered)
            self.assertNotIn("PRIVATE", rendered)
            self.assertEqual(len(first.requests), 0)
            self.assertEqual(len(second.requests), 1)
            dispatch.assert_not_awaited()

    def test_chat_reason_and_received_head_survive_body_failure_in_stderr(self):
        exchange = ExchangeFailed(
            "Cannot receive model response",
            primary=TimeoutError("PRIVATE_TIMEOUT"),
            response_head=HttpResponseHead(429, ((b"x-request-id", b"request:model-head"), (b"x-private", b"PRIVATE_HEADER"))),
        )
        chat = ChatFailed("exchange_failed", "Cannot complete interpreter request", cause=exchange)
        chat.add_note("PRIVATE_NOTE")
        report = self.receive(chat)
        rendered = json.dumps(report)
        self.assertIn('"reason": "exchange_failed"', rendered)
        self.assertIn('"status": 429', rendered)
        self.assertIn("request:model-head", rendered)
        self.assertIn("TimeoutError", rendered)
        self.assertNotIn("PRIVATE", rendered)

    def test_complete_model_response_has_correlation_without_its_body_or_other_headers(self):
        chat = ChatFailed(
            "http_status",
            "Model service returned HTTP 503",
            response=HttpResponse(
                503, ((b"x-request-id", b"request:model-complete"), (b"x-private", b"PRIVATE_HEADER")), b"PRIVATE_BODY"
            ),
        )
        report = self.receive(chat)
        rendered = json.dumps(report)
        self.assertIn('"reason": "http_status"', rendered)
        self.assertIn('"status": 503', rendered)
        self.assertIn("request:model-complete", rendered)
        self.assertNotIn("PRIVATE", rendered)

    def test_current_transport_head_is_read_at_render_time_and_invalid_ids_are_excluded(self):
        exchange = ExchangeFailed("Cannot finish model I/O")
        chat = ChatFailed("exchange_failed", "Model exchange failed", cause=exchange)
        for headers, expected in (
            (((b"x-request-id", b"request:late"),), "request:late"),
            (((b"x-request-id", b"PRIVATE_ONE"), (b"X-Request-ID", b"PRIVATE_TWO")), None),
            (((b"x-request-id", b"PRIVATE bad whitespace"),), None),
            (((b"x-request-id", b"x" * 129),), None),
        ):
            exchange.response_head = HttpResponseHead(429, headers)
            report = self.receive(chat)
            rendered = json.dumps(report)
            self.assertIn('"status": 429', rendered)
            if expected is not None:
                self.assertIn(expected, rendered)
            else:
                self.assertIsNone(report["relationships"][0]["failure"]["request_id"])
            self.assertNotIn("PRIVATE", rendered)

    def test_exhausted_correction_keeps_turn_reason_and_private_parser_diagnostic_through_cleanup(self):
        rejection = ToolRejected("Cannot prepare selected structure: correct its atom records.")
        rejection.__cause__ = InputRejected(
            "Cannot read selected coordinates", diagnostic="atom row 3 is incomplete\napi_key=PRIVATE_KEY"
        )
        exhausted = ConversationFailed("Interpreter exhausted its turn limit.", correction_failures=((2, rejection),))
        failure = PreparationRetirementFailed(
            (("reader retirement", OSError(errno.EIO, "PRIVATE_CLEANUP")),), operation_failure=exhausted
        )
        report = self.receive(failure)
        rendered = json.dumps(report)
        self.assertIn("interpreter turn 2 correction failure", rendered)
        self.assertIn("correct its atom records", rendered)
        self.assertIn("atom row 3 is incomplete", rendered)
        self.assertIn('"errno": 5', rendered)
        self.assertNotIn("PRIVATE", rendered)

    def test_native_input_diagnostic_survives_independent_cleanup_without_credentials(self):
        secret = 'configured-key"\\tail'
        diagnostic = (
            "valence rejected\n" + repr(secret) + '\napi_key="REMOTE_SECRET"\n' + "middle\n" * 3000 + "final valence detail"
        )
        rejected = InputRejected("Cannot generate this structure", diagnostic=diagnostic)
        failed = PreparationRetirementFailed(
            (("reader retirement", OSError(errno.EIO, "PRIVATE_CLEANUP")),), operation_failure=rejected
        )
        report = self.receive(failed, secrets=(secret,))
        rendered = json.dumps(report)
        self.assertIn("valence rejected", rendered)
        self.assertIn("final valence detail", rendered)
        self.assertIn("omitted_characters", rendered)
        self.assertIn("InputRejected", rendered)
        self.assertIn("reader retirement", rendered)
        self.assertIn('"errno": 5', rendered)
        for private in ("configured-key", "REMOTE_SECRET", "PRIVATE_CLEANUP"):
            self.assertNotIn(private, rendered)

    def test_upload_file_operation_cleanup_and_interruption_reach_stderr(self):
        failure = UploadFailed(
            "downloaded SHA-256 differs from the grant",
            cause=OSError(errno.ENOSPC, "PRIVATE_DISK_PATH api_key=PRIVATE_KEY"),
            cleanup_failures=(OSError(errno.EIO, "PRIVATE_CLEANUP"),),
            interruption=asyncio.CancelledError("PRIVATE_CANCEL"),
        )
        failure.grant_issued = True
        report = self.receive(failure, secrets=("PRIVATE_KEY",))
        rendered = json.dumps(report)
        self.assertIn("SHA-256 differs", rendered)
        self.assertIn('"errno": 28', rendered)
        self.assertIn('"errno": 5', rendered)
        self.assertIn("acquisition interruption", rendered)
        self.assertIn("CancelledError", rendered)
        self.assertIn('"grant_issued": true', rendered)
        self.assertNotIn("PRIVATE", rendered)

    async def test_store_response_refusals_reach_renderer_without_becoming_exception_causes(self):
        for file_output in (False, True):
            for refusal in ("status", "header", "encoding"):
                with self.subTest(file_output=file_output, refusal=refusal):
                    owner, api, _, _ = file_sources()
                    await owner.inspect()
                    headers = store_headers(b"geometry") + (
                        (b"x-request-id", b"request:store-refusal"), (b"x-private", b"PRIVATE_HEADER"),
                    )
                    status = 503 if refusal == "status" else 200
                    if refusal == "header":
                        headers = tuple((name, value) for name, value in headers if name != b"content-type")
                    elif refusal == "encoding":
                        headers += ((b"content-encoding", b"PRIVATE_ENCODING"),)
                    response = (
                        HttpFileResponse(status, headers, 8, "PRIVATE_FILE_HASH") if file_output
                        else HttpResponse(status, headers, b"PRIVATE_BODY")
                    )
                    with patch.object(owner, "_transfer", AsyncMock(return_value=response)):
                        with private_source_directory() as (root, directory):
                            with self.assertRaises(UploadFailed) as caught:
                                if file_output:
                                    await owner.acquire_file(api.member.upload_ref, directory_fd=directory, retirement_seconds=0.1)
                                else:
                                    await owner.acquire(api.member.upload_ref)
                            self.assertEqual(os.listdir(root), [])
                    failure = caught.exception
                    rendered = json.dumps(self.receive(failure))
                    self.assertIs(failure.response, response)
                    self.assertIsNone(failure.cause)
                    self.assertTrue(failure.grant_issued)
                    self.assertIs(owner.last_failure, failure)
                    self.assertIn(f'"status": {status}', rendered)
                    self.assertIn("request:store-refusal", rendered)
                    self.assertIn('"grant_issued": true', rendered)
                    expected = {"status": "store returned HTTP 503", "header": "content-type does not match",
                                "encoding": "encoded or partial store responses"}[refusal]
                    self.assertIn(expected, rendered)
                    self.assertNotIn("PRIVATE", rendered)

    async def test_actual_private_directory_refusal_reaches_the_operator_with_no_grant(self):
        owner, api, _, _ = file_sources()
        await owner.inspect()
        with private_source_directory() as (root, directory):
            os.fchmod(directory, 0o755)
            with self.assertRaises(UploadFailed) as caught:
                await owner.acquire_file("upload:sha256:" + "a" * 64, directory_fd=directory, retirement_seconds=0.1)
            report = self.receive(caught.exception)
            rendered = json.dumps(report)
            self.assertIn("mode-0700 directory", rendered)
            self.assertNotIn(root, rendered)
            self.assertEqual(api.calls, ["membership"])

    def test_actual_snapshot_read_and_close_failures_reach_stderr_without_acquisition_facts(self):
        interruption = asyncio.CancelledError("PRIVATE_INTERRUPTION")
        interruption.__cause__ = OSError(errno.ENOSPC, "PRIVATE_PREVIOUS_PATH")
        cases = ((OSError(errno.ENOSPC, "PRIVATE_SOURCE_PATH"), UploadFileReadFailed), (interruption, asyncio.CancelledError))
        for operation, expected in cases:
            with self.subTest(operation=type(operation).__name__):
                self.check_snapshot_read_failure(operation, expected)

    def check_snapshot_read_failure(self, operation, expected):
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            close = os.close

            def close_then_fail(descriptor):
                close(descriptor)
                raise OSError(errno.EIO, "PRIVATE_CLOSE_PATH")

            with (
                patch("provider_core.uploads.os.read", side_effect=operation),
                patch("provider_core.uploads.os.close", side_effect=close_then_fail),
                self.assertRaises(expected) as caught,
            ):
                read_upload_file(receipt, directory_fd=directory, maximum_bytes=8)
            report = self.receive(caught.exception)
            rendered = json.dumps(report)
            self.assertIn("Cannot read verified Upload file", rendered)
            self.assertIn("retire its reader worker", rendered)
            self.assertIn('"errno": 28', rendered)
            self.assertIn('"errno": 5', rendered)
            self.assertNotIn("grant_issued", rendered)
            self.assertNotIn("PRIVATE", rendered)

    def test_actual_snapshot_hash_refusal_names_reading_without_a_grant_claim(self):
        with private_source_directory() as (root, directory):
            receipt = snapshot_source(root, b"original")
            Path(root, receipt.name).write_bytes(b"replaced")
            with self.assertRaises(UploadFileReadFailed) as caught:
                read_upload_file(receipt, directory_fd=directory, maximum_bytes=8)
            report = self.receive(caught.exception)
            rendered = json.dumps(report)
            self.assertIn("Cannot read verified Upload file", rendered)
            self.assertIn("SHA-256 differs", rendered)
            self.assertNotIn("grant_issued", rendered)

    def test_failed_stderr_retains_original_cleanup_and_output_causes_without_file_fallback(self):
        for output_failure in (BrokenPipeError(errno.EPIPE, "PRIVATE_STDERR"), ValueError("closed stream")):
            with self.subTest(output=type(output_failure).__name__):
                primary = InputRejected("Cannot read selected input")
                cleanup = OSError(errno.EIO, "PRIVATE_CLEANUP")
                with (
                    patch("nmrnet_provider.process._write_stderr", side_effect=output_failure) as write,
                    patch("nmrnet_provider.process._publish") as publish,
                    self.assertRaises(ProviderProcessFailed) as caught,
                ):
                    _failure_report((("operation", primary), ("cleanup", cleanup)), secrets=(), held=True)
                write.assert_called_once()
                publish.assert_not_called()
                self.assertTrue(caught.exception.requires_host_retirement)
                self.assertEqual(tuple(cause for _, cause in caught.exception.failures), (primary, cleanup, output_failure))
                rendered = json.dumps(failure_evidence(caught.exception, secrets=()))
                self.assertIn("Cannot read selected input", rendered)
                self.assertIn("stderr diagnostic output", rendered)
                self.assertNotIn("PRIVATE", rendered)

    def test_exact_owned_refusal_reasons_remain_distinct_through_receiving_renderer(self):
        for failure in (
            OwnershipRefused("Attempt claim still owns an operation; settle it before another effect"),
            ExecutionRefused("Exact acknowledgement is not current in storage; reread it before preparing work"),
            StartRefused("Exact pending start is absent or has already advanced; reread storage before recovery"),
            CheckpointRefused("Producer checkpoint belongs to another recovery scope"),
            DeliveryRejected("The durable acknowledgement does not match this selected body"),
            RecoveryRefused("Selected recovery record is no longer current; reread storage before another action"),
            UploadInputRejected("inspect attached Uploads before selecting a source"),
        ):
            with self.subTest(owner=type(failure).__name__):
                report = self.receive(failure)
                self.assertIn(str(failure), json.dumps(report))

    def test_observed_server_stop_is_not_an_internal_execution_refusal(self):
        stopped = ExecutionStopped(AttemptObservation(ATTEMPT_REF, "job:example", "expired", "closed"))
        report = self.receive(stopped)
        rendered = json.dumps(report)
        self.assertIn("observed Attempt expired and Job closed", rendered)
        self.assertNotIn("ExecutionRefused", rendered)

    def test_result_shape_identity_and_atom_order_reasons_survive_cleanup(self):
        for reason in (
            "Cannot admit scientific result: executed request identity differs from the prepared geometry.",
            "Cannot admit scientific result: observed runtime metadata differs from the admitted runtime.",
            "Cannot admit scientific result: predictions do not match the prepared atom order and elements.",
        ):
            failure = ResultAssemblyFailed({"PRIVATE_RESULT": [1, 2, 3]}, b"PRIVATE_PREPARED_IDENTITY")
            failure.__cause__ = ResultRejected(reason)
            report = self.receive(
                PreparationRetirementFailed((("native cleanup", OSError(errno.EIO, "PRIVATE")),), operation_failure=failure)
            )
            rendered = json.dumps(report)
            self.assertIn(reason, rendered)
            self.assertIn('"errno": 5', rendered)
            self.assertNotIn("PRIVATE", rendered)

    async def test_received_result_is_identified_without_retaining_numeric_output_or_repeating_science(self):
        ports = SessionPorts()
        self.addAsyncCleanup(ports.session.retire)
        handle = await ports.session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        result = tuple(ports.worker.outcome(item.request_bytes)[0] for item in ports.batch.conformers)
        result[0]["prediction"]["unit"] = "PRIVATE_UNIT"
        result[0]["provenance"]["PRIVATE_PROVENANCE"] = "PRIVATE_RAW"
        with patch.object(ports.worker, "execute", new=AsyncMock(return_value=result)) as execute:
            with self.assertRaises(ResultAssemblyFailed) as caught:
                await ports.session.execute(handle, report_condition=unexpected_condition)
            failure = PreparationRetirementFailed(
                (("worker cleanup", OSError(errno.EIO, "PRIVATE_CLEANUP")),), operation_failure=caught.exception
            )
            report = self.receive(failure)
            rendered = json.dumps(report)
            for fact in (
                '"complete_payload_in_report": false',
                '"prediction_values_in_report": false',
                '"received_geometry_count": 2',
                '"prediction_row_count": 4',
                result[0]["request_sha256"],
                hashlib.sha256(handle.identity.content).hexdigest(),
                '"errno": 5',
                "ResultRejected",
            ):
                self.assertIn(fact, rendered)
            self.assertNotIn("shift_ppm", rendered)
            self.assertNotIn("PRIVATE", rendered)
            self.assertIn("memory", rendered)
            with self.assertRaises(ValueError):
                await ports.session.execute(handle, report_condition=unexpected_condition)
            execute.assert_awaited_once()

    async def test_unretained_preparation_diagnostic_has_bounded_receiving_evidence_before_prediction(self):
        ports = SessionPorts()
        self.addAsyncCleanup(ports.session.retire)
        secret = 'configured-key"\\tail'
        diagnostic = "native preparation detail\n" + repr(secret) + "\n" + "middle" * MAX_EVIDENCE_BYTES + "last detail"
        ports.generator.generate.return_value = replace(ports.batch, diagnostic=diagnostic)
        with self.assertRaises(PreparationEvidenceFailed) as caught:
            await ports.session.prepare(ports.acknowledged, ports.instructions, report_condition=unexpected_condition)
        report = self.receive(caught.exception, secrets=(secret,))
        rendered = json.dumps(report)
        self.assertIn('"complete_payload_in_report": false', rendered)
        self.assertIn("native preparation detail", rendered)
        self.assertIn("last detail", rendered)
        self.assertIn("omitted_characters", rendered)
        self.assertIn("ValueError", rendered)
        self.assertIn("memory", rendered)
        self.assertNotIn("configured-key", rendered)
        self.assertNotIn("coordinates", rendered)
        self.assertLess(len(rendered), 16000)
        self.assertEqual(ports.worker.events, [])

    def test_received_result_projection_bounds_malformed_geometry_and_row_metadata(self):
        result = tuple(
            {
                "request_sha256": "PRIVATE_HASH",
                "prediction": {"unit": "PRIVATE_UNIT", "predictions": [{"PRIVATE_ROW": [1, 2, 3]}] * 129},
                "provenance": {"PRIVATE_COORDINATES": [[1, 2, 3]]},
            }
            for _ in range(129)
        )
        report = self.receive(ResultAssemblyFailed(result, b"PRIVATE_IDENTITY"))
        rendered = json.dumps(report)
        self.assertIn('"received_geometry_count": 129', rendered)
        self.assertIn('"omitted_geometry_count": 1', rendered)
        self.assertIn('"prediction_row_count": 129', rendered)
        self.assertIn('"omitted_prediction_row_count": 1', rendered)
        self.assertIn('"inspected_prediction_row_count": 128', rendered)
        self.assertIn('"malformed_inspected_prediction_row_shape_count": 128', rendered)
        self.assertNotIn("PRIVATE", rendered)

    def test_owned_preparation_diagnostic_variants_keep_safe_facts_and_redact_even_valid_digest_secrets(self):
        secret = "a" * 64
        rejection = ToolRejected("correct the source records")
        rejection.__cause__ = InputRejected("Cannot read source", diagnostic="parser detail\napi_key=REMOTE_KEY")
        document = {
            "input_fingerprint": secret,
            "diagnostic": "preparation detail",
            "generation_failure": {"diagnostic": "generation detail", "provenance_utf8": "PRIVATE_PROVENANCE"},
            "model_report": {"explanation": "model explanation", "interpretation": "PRIVATE_TRANSCRIPT"},
            "preparation_diagnostics": [{"diagnostic": "reader detail", "coordinates": "PRIVATE_COORDINATES"}],
            "interpreter_diagnostics": [{"turn_index": 3, "failure": failure_evidence(rejection, secrets=())}],
            "PRIVATE_UNKNOWN": [1, 2, 3],
        }
        outcome = {"request_sha256": secret, "prediction": {"unit": "ppm", "predictions": []}}
        for failure in (PreparationEvidenceFailed(document), ResultAssemblyFailed((outcome,), None)):
            report = self.receive(failure, secrets=(secret,))
            rendered = json.dumps(report)
            self.assertIn("[redacted]", rendered)
            self.assertNotIn(secret, rendered)
            self.assertNotIn("PRIVATE", rendered)
            self.assertNotIn("REMOTE_KEY", rendered)
            if type(failure) is PreparationEvidenceFailed:
                for fact in (
                    "preparation detail",
                    "generation detail",
                    "model explanation",
                    "reader detail",
                    "parser detail",
                    "correct the source records",
                    '"turn_index": 3',
                    '"independently_verified": false',
                ):
                    self.assertIn(fact, rendered)

    def test_malformed_preparation_diagnostics_do_not_prevent_other_causes_from_reaching_the_operator(self):
        cyclic = {}
        cyclic["failure"] = cyclic
        document = {
            "scientific_evidence_reservation": {
                "preparation_diagnostics": [None] * 129,
                "interpreter_diagnostics": [{"failure": cyclic}] * 65,
            },
        }
        failure = PreparationEvidenceFailed(document)
        failure.__cause__ = OSError(errno.EIO, "PRIVATE_CAUSE")
        report = self.receive(failure)
        rendered = json.dumps(report)
        self.assertIn('"preparation_diagnostics_count": 129', rendered)
        self.assertIn('"preparation_diagnostics_omitted_count": 1', rendered)
        self.assertIn('"interpreter_diagnostics_count": 65', rendered)
        self.assertIn('"interpreter_diagnostics_omitted_count": 1', rendered)
        self.assertIn('"diagnostic_graph_is_json": false', rendered)
        self.assertIn('"errno": 5', rendered)
        self.assertNotIn("PRIVATE", rendered)

    def test_scientific_and_model_refusals_keep_useful_private_diagnostics_without_partial_arrays(self):
        secret = 'configured-diagnostic-key"\\tail'
        credentials = repr(secret) + '\napi_key="REMOTE_SECRET"\nBearer REMOTE_TOKEN'
        partial = GeneratedConformers(
            (GeneratedConformer(0, b"PRIVATE_COORDINATES", b"PRIVATE_STEREO"),),
            b'{"requested_count":2,"PRIVATE_PROVENANCE":"not a text diagnostic"}',
            "native embedding budget exhausted\n" + credentials + "\n" + "middle\n" * 2000 + "final embedding detail",
        )
        for failure, expected, diagnostic in (
            (ConformerShortfall(partial), "requested 2, RDKit returned 1", "native embedding budget exhausted"),
            (
                ConformerOutputLimit(
                    {
                        "conformers": [{"PRIVATE_COORDINATES": [1, 2, 3]}],
                        "provenance_utf8": "PRIVATE_PROVENANCE",
                        "diagnostic": "native conformer output could not fit\n" + credentials,
                    },
                    2,
                ),
                "complete output exceeds the 2 MiB channel limit",
                "native conformer output could not fit",
            ),
            (
                ModelReportedInputProblem(
                    _InputProblemReport("source_unit_unclear", "record 2 coordinate unit was not specified\n" + credentials),
                    b"PRIVATE_OBSERVATION",
                ),
                "Specify angstrom, nm or bohr",
                "record 2 coordinate unit was not specified",
            ),
        ):
            wrapped = PreparationRetirementFailed(
                (("cleanup", OSError(errno.EIO, "PRIVATE_CLEANUP")),), operation_failure=failure
            )
            report = self.receive(wrapped, secrets=(secret,))
            rendered = json.dumps(report)
            self.assertIn(expected, rendered)
            self.assertIn(diagnostic, rendered)
            self.assertIn('"errno": 5', rendered)
            for private in (
                "configured-diagnostic-key",
                "REMOTE_SECRET",
                "REMOTE_TOKEN",
                "PRIVATE_COORDINATES",
                "PRIVATE_STEREO",
                "PRIVATE_PROVENANCE",
                "PRIVATE_OBSERVATION",
                "PRIVATE_CLEANUP",
            ):
                self.assertNotIn(private, rendered)
            if type(failure) is ConformerShortfall:
                self.assertIn("final embedding detail", rendered)
                self.assertIn("omitted_characters", rendered)
            if type(failure) is ModelReportedInputProblem:
                self.assertIn('"independently_verified": false', rendered)

    def test_credential_parser_cause_is_traversed_without_rendering_its_private_text(self):
        failure = CredentialRejected("use the original API-issued document", cause=ValueError("PRIVATE_KEY_PARSER"))
        report = self.receive(failure)
        rendered = json.dumps(report)
        self.assertIn("original API-issued document", rendered)
        self.assertIn("ValueError", rendered)
        self.assertNotIn("PRIVATE", rendered)

    def test_start_enqueue_and_api_receipt_presence_remain_distinct_without_cleanup_or_deletion_claims(self):
        active = acknowledge_start(record(), admission())
        item = DeliveryOutcome(active.start.scope.api_origin, active.start.scope.provider_ref,
            active.admission.job_ref, ATTEMPT_REF, TerminalOperation.FAIL,
            fail_body(ATTEMPT_REF, "input_rejected", "PRIVATE_MESSAGE"))
        for acknowledgement in (None, active):
            failure = StartFailed("Start acknowledgement durability is unconfirmed",
                cause=OSError(errno.EIO, "PRIVATE"), acknowledgement=acknowledgement,
                api_failure=None, settlement_failures=())
            report = self.receive(failure)
            owner = report["relationships"][0]["failure"]
            self.assertEqual(owner["acknowledgement_received"], acknowledgement is active)
            if acknowledgement is active:
                self.assertEqual(owner["observed_attempt_state"], "in_progress")
            self.assertNotIn("PRIVATE", json.dumps(report))
        for acceptance in (None, item.acceptance()):
            failure = ExecutionFailed(cause=CheckpointFailed("Cannot retain accepted control", cause=OSError(errno.EIO, "PRIVATE")),
                outcome=None, unconfirmed_record=None, api_failure=None, cleanup_failures=(), attempt=active)
            failure.delivery_acceptance = acceptance
            report = self.receive(failure)
            owner = report["relationships"][0]["failure"]
            self.assertEqual(owner["delivery_acceptance_received"], acceptance is not None)
            self.assertNotIn("api_receipt_received", owner)
            self.assertNotIn("retirement_confirmed", owner)
            self.assertNotIn("PRIVATE", json.dumps(report))
        for received in (False, True):
            failure = DeliveryWorkerFailed(
                cause=DeliveryStorageFailed("Cannot remove the published queue row", cause=OSError(errno.EIO, "PRIVATE_DISK")),
                api_failure=None, cleanup_failures=(OSError(errno.EPIPE, "PRIVATE_CLEANUP"),),
                api_receipt_received=received,
            )
            report = self.receive(failure)
            owner = report["relationships"][0]["failure"]
            self.assertIs(owner["api_receipt_received"], received)
            rendered = json.dumps(report)
            self.assertIn('"errno": 5', rendered)
            self.assertIn('"errno": 32', rendered)
            self.assertNotIn("delivery_acceptance_received", owner)
            self.assertNotIn("queue_row_deleted", owner)
            self.assertNotIn("PRIVATE", rendered)
