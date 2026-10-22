"""Core projects owned facts; consumers retain redaction and graph traversal."""

import asyncio
import unittest

from provider_core.api import ApiFailure
from provider_core.attempt_record import (
    ProtectedEvidence,
    TerminalConfirmed,
    TerminalHeld,
    TerminalOperation,
    TerminalPending,
    acknowledge_start,
)
from provider_core.attempt_request import fail_body
from provider_core.attempt_response import read_fail
from provider_core.chat import ChatFailed
from provider_core.conversation import ConversationRetirementFailed, ToolRejected
from provider_core.execution import ExecutionFailed
from provider_core.failure_projection import owned_details, owned_relationships
from provider_core.publication import PublicationFailed
from provider_core.starting import StartFailed
from provider_core.transport import ExchangeFailed, HttpResponse, HttpResponseHead
from provider_core.uploads import UploadFailed, UploadFileReadFailed, UploadInputRejected
from tests.fakes.attempt_store import admission, record
from tests.fakes.attempts import ATTEMPT_REF, fail_response


class CoreFailureProjectionTests(unittest.TestCase):
    def test_api_projects_received_correlation_and_distinct_effect_flags_without_payloads(self):
        response = HttpResponse(
            503,
            ((b"x-request-id", b"request:received"), (b"authorization", b"PRIVATE_HEADER")),
            b"PRIVATE_BODY",
        )
        failure = ApiFailure("read owned state", "the API could not admit this read", response=response)
        failure.issuance_may_have_committed = True
        details = owned_details(failure)
        self.assertEqual(
            details,
            {
                "message": str(failure),
                "operation": "read owned state",
                "status": 503,
                "request_id": "request:received",
                "problem_code": None,
                "issuance_may_have_committed": True,
                "attempt_mutation_may_have_committed": False,
            },
        )
        self.assertNotIn("PRIVATE", repr(details))
        details["status"] = 200
        self.assertEqual(owned_details(failure)["status"], 503)

    def test_chat_uses_complete_response_before_transport_head_and_reads_late_metadata(self):
        exchange = ExchangeFailed("Response body did not finish", primary=TimeoutError())
        failure = ChatFailed("exchange_failed", "Cannot complete this model exchange", cause=exchange)
        self.assertNotIn("status", owned_details(failure))
        exchange.response_head = HttpResponseHead(429, ((b"x-request-id", b"request:head"), (b"set-cookie", b"PRIVATE")))
        self.assertEqual(owned_details(failure)["status"], 429)
        self.assertEqual(owned_details(failure)["request_id"], "request:head")
        failure.response = HttpResponse(503, ((b"x-request-id", b"request:complete"),), b"PRIVATE_BODY")
        self.assertEqual(owned_details(failure)["status"], 503)
        self.assertEqual(owned_details(failure)["request_id"], "request:complete")
        self.assertNotIn("PRIVATE", repr(owned_details(failure)))
        exchange.response_head = HttpResponseHead(429, ((b"x-request-id", b"one"), (b"x-request-id", b"two")))
        self.assertIsNone(owned_details(exchange)["request_id"])

    def test_transport_edges_preserve_original_identity_order_and_independent_cleanup(self):
        primary, cleanup, interrupted = TimeoutError(), OSError(), asyncio.CancelledError()
        failure = ExchangeFailed("Interrupted response", primary=primary, cleanup_failures=(cleanup,), interruption=interrupted)
        self.assertEqual(
            list(owned_relationships(failure)),
            [
                ("independent cleanup", cleanup),
                ("transport primary", primary),
                ("transport interruption", interrupted),
            ],
        )

    def test_conversation_edges_preserve_endpoint_corrections_primary_and_interruption(self):
        endpoint, cleanup, cause, previous = TimeoutError(), OSError(), RuntimeError(), asyncio.CancelledError()
        correction = ToolRejected("Choose an advertised operation")
        failure = ConversationRetirementFailed(
            ((2, cleanup),),
            cause=cause,
            endpoint_failures=((0, endpoint),),
            correction_failures=((1, correction),),
        )
        failure.interruption_cause = previous
        self.assertEqual(
            list(owned_relationships(failure)),
            [
                ("interpreter endpoint 2 cleanup", cleanup),
                ("interpreter endpoint 0 failure", endpoint),
                ("interpreter turn 1 correction failure", correction),
                ("owned cause", cause),
                ("interruption cause", previous),
            ],
        )

    def test_acquisition_and_reader_failures_keep_separate_grant_and_interruption_facts(self):
        cause, cleanup, interrupted = OSError(), OSError(), asyncio.CancelledError()
        for kind in (UploadFailed, UploadInputRejected):
            with self.subTest(kind=kind):
                failure = kind(
                    "Source transfer did not finish", cause=cause, cleanup_failures=(cleanup,), interruption=interrupted
                )
                failure.grant_issued = True
                self.assertTrue(owned_details(failure)["grant_issued"])
                self.assertEqual(
                    list(owned_relationships(failure)),
                    [
                        ("owned cause", cause),
                        ("independent cleanup", cleanup),
                        ("acquisition interruption", interrupted),
                    ],
                )
        reader = UploadFileReadFailed(
            "The acquired source changed", cause=cause, cleanup_failures=(cleanup,), interruption=interrupted
        )
        self.assertNotIn("grant_issued", owned_details(reader))
        self.assertEqual(
            list(owned_relationships(reader)),
            [
                ("owned cause", cause),
                ("independent cleanup", cleanup),
                ("source reader interruption", interrupted),
            ],
        )

    def test_received_start_and_terminal_facts_do_not_infer_durability_or_outcome_acceptance(self):
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
        held = TerminalHeld(pending, "execution_attempt_outcome_expired", "request:held")
        for acknowledgement in (None, active):
            failure = StartFailed(
                "Start storage is unconfirmed",
                cause=OSError(),
                acknowledgement=acknowledgement,
                api_failure=None,
                settlement_failures=(),
            )
            details = owned_details(failure)
            self.assertEqual(details["acknowledgement_received"], acknowledgement is active)
            self.assertEqual("observed_attempt_state" in details, acknowledgement is active)
        for outcome in (None, confirmed, held):
            failure = PublicationFailed(
                "Outcome storage is unconfirmed", cause=OSError(), outcome=outcome, api_failure=None, settlement_failures=()
            )
            details = owned_details(failure)
            self.assertEqual(details["publication_observation_received"], outcome is not None)
            self.assertEqual(details["terminal_receipt_received"], outcome is confirmed)
            self.assertNotIn("PRIVATE", repr(details))
        execution = ExecutionFailed(
            cause=OSError(), outcome=confirmed, unconfirmed_record=pending, api_failure=None, cleanup_failures=()
        )
        details = owned_details(execution)
        self.assertTrue(details["outcome_in_memory"])
        self.assertTrue(details["unconfirmed_record_in_memory"])
        self.assertNotIn("PRIVATE", repr(details))
