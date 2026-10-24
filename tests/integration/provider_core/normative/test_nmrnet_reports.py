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
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from tests.fakes.acquisition import file_sources, private_source_directory, snapshot_source
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import admission, record
from tests.fakes.attempts import ATTEMPT_REF, fail_response, observation, progress_response
from tests.fakes.condition_recovery import ConditionOutageTransport
from tests.fakes.conditions import ConditionTransport, ReportingSession, SwallowingSession
from tests.fakes.controller import empty_store
from tests.fakes.execution import Session, acknowledged_store, unexpected_condition
from tests.fakes.job_input import response
from tests.fakes.publication import api
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
from provider_core.attempt_record import (
    MAX_EVIDENCE_BYTES,
    AcknowledgedAttempt,
    LocalExecutionPhase,
    ProtectedEvidence,
    TerminalConfirmed,
    TerminalHeld,
    TerminalOperation,
    TerminalPending,
    TerminalReadNotVisible,
    TerminalReadRequired,
    TerminalStateObserved,
    acknowledge_start,
)
from provider_core.attempt_request import fail_body
from provider_core.attempt_response import AttemptObservation, read_fail
from provider_core.attempt_store import StoreFailed
from provider_core.chat import ChatEndpoint, ChatEndpointSpec, ChatFailed
from provider_core.controller import StartRecovery, TerminalRecovery
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
from provider_core.publication import PublicationFailed, PublicationRefused
from provider_core.recovery import RecoveryFailed, RecoveryRefused
from provider_core.starting import StartFailed, StartRefused
from provider_core.transport import ExchangeFailed, HttpResponse, HttpResponseHead
from provider_core.uploads import UploadFailed, UploadFileReadFailed, UploadInputRejected, read_upload_file


class ReceivingReportTests(unittest.IsolatedAsyncioTestCase):
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

    def test_history_expiry_preserves_observed_unlinks_native_cause_and_unsynced_directory_for_operator(self):
        failure = StoreFailed(
            "Confirmed-history expiry is unconfirmed",
            cause=OSError(errno.EIO, "owned key=credential-canary"),
            removed_history_records=("a" * 64 + ".json",),
            uncertain_history_record="b" * 64 + ".json",
            history_directory_synced=False,
            history_trim_phase="unlink",
        )
        rendered = json.dumps(self.receive(failure, secrets=("credential-canary",)))
        self.assertIn("removed_history_records", rendered)
        self.assertIn("a" * 64 + ".json", rendered)
        self.assertIn("b" * 64 + ".json", rendered)
        self.assertIn('"history_directory_synced": false', rendered)
        self.assertIn('"history_trim_phase": "unlink"', rendered)
        self.assertIn('"errno": 5', rendered)
        self.assertNotIn("credential-canary", rendered)

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
                await executor.retain_suppressed_outcome()
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
                api=api(transport, store.scope.api_origin),
                store=store,
                claims=AttemptClaims(),
                execution_identity={},
                selected_artifacts={},
                policy=NativePolicy(allocation_seconds=1, prediction_seconds=1, source_seconds=1),
                observation_recovery=ObservationRecovery(1, 1, Mock(), Mock()),
                condition_recovery=ConditionRecovery(1, 1, Mock(), Mock()),
                on_prediction_retry=Mock(),
                on_native_notice=Mock(),
                on_unconfirmed_phase_progress=Mock(),
                terminal_recovery=TerminalRecovery(1, Mock(), Mock()),
                start_recovery=StartRecovery(1, Mock(), Mock()),
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
                self.assertEqual(await store.records(), ())
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
        pending = TerminalPending(
            acknowledge_start(record(), admission()),
            TerminalOperation.FAIL,
            fail_body(ATTEMPT_REF, "input_rejected", "PRIVATE_MESSAGE"),
            ProtectedEvidence("evidence.v1", b"PRIVATE_EVIDENCE"),
        )
        required = TerminalReadRequired(pending, 409, "request:publication")
        observed = TerminalStateObserved(required, AttemptObservation(ATTEMPT_REF, "job:example", "succeeded", "closed"))
        invisible = TerminalReadNotVisible(required, "request:read")
        send = ApiFailure(
            "publish outcome",
            "No verified response was obtained",
            response=HttpResponse(
                409, ((b"x-request-id", b"request:publication"), (b"authorization", b"PRIVATE_HEADER")), b"PRIVATE_BODY"
            ),
        )
        publication = PublicationFailed(
            "Publication stopped for read-only reconciliation",
            cause=send,
            outcome=required,
            api_failure=send,
            settlement_failures=(),
        )
        report = self.receive(publication)
        rendered = json.dumps(report)
        self.assertIn('"terminal_receipt_received": false', rendered)
        self.assertIn('"publication_outcome_kind": "TerminalReadRequired"', rendered)
        self.assertIn("request:publication", rendered)
        self.assertNotIn("PRIVATE", rendered)
        for result in (observed, invisible):
            failure = RecoveryFailed(OSError(errno.EIO, "PRIVATE_PATH"), observed_record=result)
            report = self.receive(failure)
            rendered = json.dumps(report)
            self.assertIn('"terminal_readback_in_memory": true', rendered)
            self.assertIn('"errno": 5', rendered)
            if result is observed:
                self.assertIn('"observed_attempt_state": "succeeded"', rendered)
            else:
                self.assertIn('"terminal_read_not_visible": true', rendered)
                self.assertIn("request:read", rendered)
            self.assertNotIn("PRIVATE", rendered)
            self.assertNotIn("terminal_receipt_received", rendered)
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
                allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
            )
            try:
                with self.assertRaises(ExecutionFailed) as caught:
                    await executor.run(acknowledged)
                self.assertIs(caught.exception.outcome, session.outcome)
                (retained,) = await store.records()
                self.assertIs(type(retained), AcknowledgedAttempt)
                self.assertIs(retained.local_phase, LocalExecutionPhase.EXECUTION_ENTERED)
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
                await executor.retain_suppressed_outcome()
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
            PublicationRefused("Terminal record belongs to another recovery scope"),
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

    def test_received_start_and_terminal_receipts_are_presence_facts_without_durability_claims(self):
        active = acknowledge_start(record(), admission())
        pending = TerminalPending(
            active,
            TerminalOperation.FAIL,
            fail_body(ATTEMPT_REF, "input_rejected", " Explain\n雪\x01 "),
            ProtectedEvidence("evidence.v1", b"PRIVATE_EVIDENCE"),
        )
        receipt = read_fail(
            fail_response(), execution_attempt_ref=ATTEMPT_REF, failure_code="input_rejected", failure_message=" Explain\n雪\x01 "
        )
        confirmed = TerminalConfirmed(pending, receipt)
        held = TerminalHeld(pending, "execution_attempt_outcome_expired", "request:conflict")
        for acknowledgement in (None, active):
            failure = StartFailed(
                "Start acknowledgement durability is unconfirmed",
                cause=OSError(errno.EIO, "PRIVATE"),
                acknowledgement=acknowledgement,
                api_failure=None,
                settlement_failures=(),
            )
            report = self.receive(failure)
            owner = report["relationships"][0]["failure"]
            self.assertEqual(owner["acknowledgement_received"], acknowledgement is active)
            if acknowledgement is active:
                self.assertEqual(owner["observed_attempt_state"], "in_progress")
            self.assertNotIn("PRIVATE", json.dumps(report))
        for outcome in (None, confirmed, held):
            failure = PublicationFailed(
                "Publication receipt durability is unconfirmed",
                cause=OSError(errno.EIO, "PRIVATE"),
                outcome=outcome,
                api_failure=None,
                settlement_failures=(),
            )
            report = self.receive(failure)
            owner = report["relationships"][0]["failure"]
            self.assertEqual(owner["publication_observation_received"], outcome is not None)
            self.assertEqual(owner["terminal_receipt_received"], outcome is confirmed)
            if outcome is not None:
                self.assertEqual(owner["publication_outcome_kind"], type(outcome).__name__)
                self.assertEqual(owner["terminal_operation"], "fail")
            self.assertNotIn("PRIVATE", json.dumps(report))
