"""Operator diagnostics are read from the real stderr/file receiving surfaces."""

import asyncio
import errno
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.fakes.attempt_store import admission, record
from tests.fakes.attempts import ATTEMPT_REF, fail_response
from tests.fakes.tls_peer import TlsPeer
from tests.integration.nmrnet.interpreter_ports import chat_endpoint
from tests.integration.nmrnet.smiles_ports import SessionPorts

from nmrnet_provider.allocation import AllocationUnconfirmed
from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.input_preparation import PreparationRetirementFailed
from nmrnet_provider.interpreter import ModelReportedInputProblem, _InputProblemReport
from nmrnet_provider.process import _failure_report
from nmrnet_provider.process_reports import ProviderProcessFailed
from nmrnet_provider.result import ResultRejected
from nmrnet_provider.retirement import HostRetirement, RetirementUnconfirmed
from nmrnet_provider.session import ResultAssemblyFailed
from nmrnet_provider.smiles import ConformerShortfall, GeneratedConformer, GeneratedConformers
from nmrnet_provider.smiles_protocol import ConformerOutputLimit
from provider_core.attempt_record import (
    ProtectedEvidence,
    TerminalConfirmed,
    TerminalHeld,
    TerminalOperation,
    TerminalPending,
    acknowledge_start,
)
from provider_core.attempt_request import fail_body
from provider_core.attempt_response import AttemptObservation, read_fail
from provider_core.chat import ChatFailed
from provider_core.conversation import ConversationFailed, ToolConversation, ToolRejected
from provider_core.credential import CredentialRejected
from provider_core.execution import ExecutionRefused, ExecutionStopped
from provider_core.ownership import OwnershipRefused
from provider_core.publication import PublicationFailed, PublicationRefused
from provider_core.recovery import RecoveryRefused
from provider_core.starting import StartFailed, StartRefused
from provider_core.transport import ExchangeFailed, HttpResponse, HttpResponseHead
from provider_core.uploads import UploadInputRejected


class ReceivingReportTests(unittest.IsolatedAsyncioTestCase):
    # silent-defaults: allow-contract -- Fixtures without configured credentials still exercise the same receiving renderer; credential cases supply explicit keys.
    def receive(self, failure, *, secrets=()):
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stderr(output):
                _failure_report(Path(directory), (("operation", failure),), secrets=secrets, held=False)
            return json.loads(output.getvalue()), json.loads((Path(directory) / "failure.json").read_bytes())

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
        for received in self.receive(caught.exception):
            rendered = json.dumps(received)
            self.assertIn("model could not identify the source unit", rendered)
            self.assertIn('"independently_verified": false', rendered)
            self.assertIn('"errno": 5', rendered)
            self.assertNotIn("PRIVATE", rendered)
        self.assertEqual(ports.worker.events, ["close", "closed"])
        self.assertTrue(ports.host.confirmed)

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
            for report in self.receive(caught.exception, secrets=("PRIVATE_MODEL_KEY",)):
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

    def test_chat_reason_and_received_head_survive_body_failure_in_both_operator_outputs(self):
        exchange = ExchangeFailed(
            "Cannot receive model response",
            primary=TimeoutError("PRIVATE_TIMEOUT"),
            response_head=HttpResponseHead(429, ((b"x-request-id", b"request:model-head"), (b"x-private", b"PRIVATE_HEADER"))),
        )
        chat = ChatFailed("exchange_failed", "Cannot complete interpreter request", cause=exchange)
        chat.add_note("PRIVATE_NOTE")
        for report in self.receive(chat):
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
        for report in self.receive(chat):
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
            for report in self.receive(chat):
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
        for report in self.receive(failure):
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
        for report in self.receive(failed, secrets=(secret,)):
            rendered = json.dumps(report)
            self.assertIn("valence rejected", rendered)
            self.assertIn("final valence detail", rendered)
            self.assertIn("omitted_characters", rendered)
            self.assertIn("InputRejected", rendered)
            self.assertIn("reader retirement", rendered)
            self.assertIn('"errno": 5', rendered)
            for private in ("configured-key", "REMOTE_SECRET", "PRIVATE_CLEANUP"):
                self.assertNotIn(private, rendered)

    def test_broken_stderr_still_publishes_original_cleanup_and_reporting_cause_to_file(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = io.StringIO()
            with (
                patch.object(broken, "write", side_effect=BrokenPipeError(errno.EPIPE, "PRIVATE_STDERR")),
                redirect_stderr(broken),
            ):
                _failure_report(
                    Path(directory),
                    (
                        ("original operation", InputRejected("Cannot read selected input")),
                        ("original cleanup", OSError(errno.EIO, "PRIVATE_CLEANUP")),
                    ),
                    secrets=(),
                    held=True,
                )
            report = json.loads((Path(directory) / "failure.json").read_bytes())
            rendered = json.dumps(report)
            self.assertIn("original operation", rendered)
            self.assertIn("original cleanup", rendered)
            self.assertIn("stderr", rendered)
            self.assertIn('"errno": 32', rendered)
            self.assertIn('"requires_host_retirement": true', rendered)
            self.assertNotIn("PRIVATE", rendered)

    def test_both_failed_sinks_keep_original_and_reporting_failures_in_the_owned_error(self):
        with tempfile.TemporaryDirectory() as directory:
            broken = io.StringIO()
            primary = InputRejected("Cannot read selected input")
            cleanup = OSError(errno.EIO, "PRIVATE_CLEANUP")
            stderr_failure = BrokenPipeError(errno.EPIPE, "PRIVATE_STDERR")
            file_failure = PermissionError(errno.EACCES, "PRIVATE_FILE")
            with (
                patch.object(broken, "write", side_effect=stderr_failure),
                redirect_stderr(broken),
                patch("nmrnet_provider.process._publish", side_effect=file_failure) as publish,
                self.assertRaises(ProviderProcessFailed) as caught,
            ):
                _failure_report(Path(directory), (("operation", primary), ("cleanup", cleanup)), secrets=(), held=True)
            publish.assert_called_once()
            self.assertTrue(caught.exception.requires_host_retirement)
            self.assertEqual(
                tuple(cause for _, cause in caught.exception.failures), (primary, cleanup, stderr_failure, file_failure)
            )

    def test_closed_stderr_still_publishes_original_failures_and_closed_stream_type(self):
        with tempfile.TemporaryDirectory() as directory:
            closed = io.StringIO()
            closed.close()
            with redirect_stderr(closed):
                _failure_report(
                    Path(directory),
                    (("original operation", InputRejected("Cannot read selected input")),),
                    secrets=(),
                    held=False,
                )
            report = json.loads((Path(directory) / "failure.json").read_bytes())
            rendered = json.dumps(report)
            self.assertIn("original operation", rendered)
            self.assertIn("stderr diagnostic output", rendered)
            self.assertIn("ValueError", rendered)

    def test_exact_owned_refusal_reasons_remain_distinct_through_receiving_renderer(self):
        for failure in (
            AllocationUnconfirmed(
                "The host did not confirm allocation within its observation budget; retire the exact request before replacement."
            ),
            OwnershipRefused("Attempt claim still owns an operation; settle it before another effect"),
            ExecutionRefused("Exact acknowledgement is not current in storage; reread it before preparing work"),
            StartRefused("Exact pending start is absent or has already advanced; reread storage before recovery"),
            PublicationRefused("Terminal record belongs to another recovery scope"),
            RecoveryRefused("Selected recovery record is no longer current; reread storage before another action"),
            UploadInputRejected("inspect attached Uploads before selecting a source"),
        ):
            with self.subTest(owner=type(failure).__name__):
                for report in self.receive(failure):
                    self.assertIn(str(failure), json.dumps(report))

    def test_observed_server_stop_is_not_an_internal_execution_refusal(self):
        stopped = ExecutionStopped(AttemptObservation(ATTEMPT_REF, "job:example", "expired", "closed"))
        for report in self.receive(stopped):
            rendered = json.dumps(report)
            self.assertIn("observed Attempt expired and Job closed", rendered)
            self.assertNotIn("ExecutionRefused", rendered)

    def test_real_retirement_receipt_reasons_survive_without_paths_or_token_echo(self):
        for receipt, expected in (
            (None, "did not publish retirement confirmation"),
            (b"PRIVATE malformed", "receipt is malformed"),
            (b"unconfirmed " + b"a" * 32 + b"\n", "host could not confirm worker retirement"),
        ):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                if receipt is not None:
                    (root / "result").write_bytes(receipt)
                owner = HostRetirement(allocation="a" * 32, request_directory=root, receipt_directory=root, operation_seconds=1)
                with (
                    patch("nmrnet_provider.retirement.time.monotonic", side_effect=(0, 0, 2)),
                    patch("nmrnet_provider.retirement.time.sleep"),
                    self.assertRaises(RetirementUnconfirmed) as caught,
                ):
                    owner._run(False)
                for report in self.receive(caught.exception):
                    rendered = json.dumps(report)
                    self.assertIn(expected, rendered)
                    self.assertNotIn("PRIVATE", rendered)

    def test_result_shape_identity_and_atom_order_reasons_survive_cleanup(self):
        for reason in (
            "Cannot admit scientific result: executed request identity differs from the prepared geometry.",
            "Cannot admit scientific result: observed runtime metadata differs from the admitted runtime.",
            "Cannot admit scientific result: predictions do not match the prepared atom order and elements.",
        ):
            failure = ResultAssemblyFailed({"PRIVATE_RESULT": [1, 2, 3]}, b"PRIVATE_PREPARED_IDENTITY")
            failure.__cause__ = ResultRejected(reason)
            for report in self.receive(
                PreparationRetirementFailed((("native cleanup", OSError(errno.EIO, "PRIVATE")),), operation_failure=failure)
            ):
                rendered = json.dumps(report)
                self.assertIn(reason, rendered)
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
            for report in self.receive(wrapped, secrets=(secret,)):
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
        for report in self.receive(failure):
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
            for report in self.receive(failure):
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
            for report in self.receive(failure):
                owner = report["relationships"][0]["failure"]
                self.assertEqual(owner["publication_observation_received"], outcome is not None)
                self.assertEqual(owner["terminal_receipt_received"], outcome is confirmed)
                if outcome is not None:
                    self.assertEqual(owner["publication_outcome_kind"], type(outcome).__name__)
                    self.assertEqual(owner["terminal_operation"], "fail")
                self.assertNotIn("PRIVATE", json.dumps(report))
