"""Publication failures retain exact effect evidence and cannot steal ownership."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from provider_core.api import ApiFailure
from provider_core.attempt_record import TerminalConfirmed, TerminalHeld, TerminalReadRequired
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.publication import PublicationFailed, PublicationRefused, TerminalPublisher, TerminalRefusalPolicy
from provider_core.transport import ExchangeFailed, HttpResponseHead
from tests.fakes.api import Transport, reply, response_headers
from tests.fakes.attempt_store import scope
from tests.fakes.attempts import ATTEMPT_REF, fail_response
from tests.fakes.publication import api, retained_failure, terminal_refusal


class PublicationFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_read_required_write_keeps_original_failure_without_polling_growth(self):
        for defect in (OSError("PRIVATE_STORAGE"), RuntimeError("PRIVATE_DEFECT")):
            with self.subTest(kind=type(defect).__name__), tempfile.TemporaryDirectory() as directory:
                store, pending = await retained_failure(directory, scope())
                claim = AttemptClaims().claim_record(pending)
                transport = Transport(terminal_refusal(409))
                publisher = TerminalPublisher(
                    api(transport, scope().api_origin), store, claim, refusal_policy=TerminalRefusalPolicy.READ_STATE
                )
                try:
                    with patch.object(store, "replace", side_effect=defect) as writes:
                        with self.assertRaises(PublicationFailed) as failure:
                            await publisher.publish(pending)
                        self.assertIs(failure.exception.cause, defect)
                        self.assertIsInstance(failure.exception.outcome, TerminalReadRequired)
                        for _ in range(3):
                            with self.assertRaises(PublicationFailed) as settled:
                                await publisher.settle()
                            self.assertIs(settled.exception, failure.exception)
                            self.assertEqual(settled.exception.settlement_failures, ())
                        writes.assert_awaited_once()
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    self.assertEqual(await store.records(), (pending,))
                    self.assertEqual(len(transport.calls), 1)
                finally:
                    await store.close()

    async def test_read_only_policy_does_not_reclassify_other_statuses(self):
        for status in (400, 401, 403, 500, 503):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                store, pending = await retained_failure(directory, scope())
                claim = AttemptClaims().claim_record(pending)
                transport = Transport(reply(b"{}", status=status))
                try:
                    publisher = TerminalPublisher(
                        api(transport, scope().api_origin), store, claim, refusal_policy=TerminalRefusalPolicy.READ_STATE
                    )
                    with self.assertRaises(PublicationFailed):
                        await publisher.publish(pending)
                    self.assertEqual(await store.records(), (pending,))
                    self.assertEqual(len(transport.calls), 1)
                    claim.release()
                finally:
                    await store.close()

    async def test_read_only_policy_retains_original_unclassified_send_failure_after_later_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            store, pending = await retained_failure(directory, scope())
            claim = AttemptClaims().claim_record(pending)
            transport = Transport(reply(b"PRIVATE_MALFORMED_BODY", status=409))
            client = api(transport, scope().api_origin)
            publisher = TerminalPublisher(client, store, claim, refusal_policy=TerminalRefusalPolicy.READ_STATE)
            try:
                result = await publisher.publish(pending)
                original = client.last_failure
                client.last_failure = ApiFailure("later call", "Different failure")
                self.assertIs(publisher.last_failure.cause, original)
                self.assertIs(publisher.last_failure.api_failure, original)
                self.assertEqual(publisher.last_failure.outcome, result)
                self.assertNotIn("PRIVATE_MALFORMED_BODY", str(publisher.last_failure))
                claim.release()
            finally:
                await store.close()

    async def test_read_only_retention_does_not_hide_unresolved_transport_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            store, pending = await retained_failure(directory, scope())
            claim = AttemptClaims().claim_record(pending)
            primary, cleanup = TimeoutError(), OSError()
            exchange = ExchangeFailed(
                "Response body and retirement failed",
                primary=primary,
                cleanup_failures=(cleanup,),
                response_head=HttpResponseHead(404, response_headers(status=404, request_id=b"request:head")),
            )
            transport = Transport(exchange)
            transport.retirement_confirmed = False
            publisher = TerminalPublisher(
                api(transport, scope().api_origin), store, claim, refusal_policy=TerminalRefusalPolicy.READ_STATE
            )
            try:
                with self.assertRaises(PublicationFailed) as failure:
                    await publisher.publish(pending)
                (required,) = await store.records()
                self.assertIsInstance(required, TerminalReadRequired)
                self.assertIs(failure.exception.api_failure.cause, exchange)
                self.assertEqual(failure.exception.outcome, required)
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                retirement = ExchangeFailed("Original transport retirement is still unconfirmed", primary=OSError())
                with (
                    patch.object(transport, "retire", side_effect=retirement),
                    self.assertRaises(PublicationFailed) as settlement,
                ):
                    await publisher.settle()
                self.assertEqual(settlement.exception.outcome, required)
                self.assertIs(settlement.exception.cause, failure.exception.cause)
                self.assertEqual(settlement.exception.settlement_failures, (retirement,))
                await publisher.settle()
                self.assertEqual(len(transport.calls), 1)
                claim.release()
            finally:
                await store.close()

    async def test_read_only_policy_keeps_reviewed_operation_conflicts_held(self):
        with tempfile.TemporaryDirectory() as directory:
            store, pending = await retained_failure(directory, scope())
            claim = AttemptClaims().claim_record(pending)
            transport = Transport(terminal_refusal(409, code="execution_attempt_outcome_expired"))
            try:
                result = await TerminalPublisher(
                    api(transport, scope().api_origin), store, claim, refusal_policy=TerminalRefusalPolicy.READ_STATE
                ).publish(pending)
                self.assertIsInstance(result, TerminalHeld)
                self.assertEqual(await store.records(), (result,))
                self.assertEqual(len(transport.calls), 1)
                claim.release()
            finally:
                await store.close()

    async def test_missing_durable_evidence_or_wrong_origin_sends_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            store, terminal = await retained_failure(directory, scope())
            transport = Transport()
            claim = AttemptClaims().claim(scope(), ATTEMPT_REF)
            try:
                with self.assertRaises(ValueError):
                    TerminalPublisher(api(transport, "https://foreign.example"), store, claim)
                publisher = TerminalPublisher(api(transport, scope().api_origin), store, claim)
                held = TerminalHeld(terminal, "execution_attempt_outcome_expired", None)
                await store.replace(terminal, held)
                for candidate in (terminal, held):
                    with self.assertRaises(PublicationRefused):
                        await publisher.publish(candidate)
                self.assertEqual(transport.calls, [])
                claim.release()
            finally:
                await store.close()

    async def test_conflict_is_durable_and_cannot_be_resent(self):
        problem = json.dumps(
            {
                "type": "urn:nmr-api:problem:operation-conflict",
                "title": "private-canary",
                "detail": "private-canary",
                "status": 409,
                "code": "execution_attempt_outcome_expired",
                "instance": "/provider/v1/execution-attempts/fail",
                "request_id": "request:example",
            }
        ).encode()
        transport = Transport(reply(problem, status=409, headers=response_headers(status=409, request_id=b"request:example")))
        with tempfile.TemporaryDirectory() as directory:
            store, terminal = await retained_failure(directory, scope())
            claim = AttemptClaims().claim(scope(), ATTEMPT_REF)
            try:
                publisher = TerminalPublisher(api(transport, scope().api_origin), store, claim)
                held = await publisher.publish(terminal)
                self.assertIsInstance(held, TerminalHeld)
                self.assertEqual(await store.records(), (held,))
                with self.assertRaises(PublicationRefused):
                    await publisher.publish(terminal)
                self.assertEqual(len(transport.calls), 1)
                claim.release()
            finally:
                await store.close()

    async def test_cancelled_receipt_write_keeps_claim_until_original_write_settles(self):
        with tempfile.TemporaryDirectory() as directory:
            store, terminal = await retained_failure(directory, scope())
            transport = Transport(reply(fail_response()))
            claim = AttemptClaims().claim(scope(), ATTEMPT_REF)
            publisher = TerminalPublisher(api(transport, scope().api_origin), store, claim)
            reached, release = threading.Event(), threading.Event()
            original = os.replace

            def delayed_replace(*args, **kwargs):
                reached.set()
                if not release.wait(2):
                    raise TimeoutError("test replacement was not released")
                return original(*args, **kwargs)

            try:
                with patch("provider_core.attempt_files.os.replace", side_effect=delayed_replace):
                    task = asyncio.create_task(publisher.publish(terminal))
                    self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIsInstance(publisher.last_failure.outcome, TerminalConfirmed)
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    with self.assertRaises(PublicationRefused):
                        await publisher.publish(terminal)
                    release.set()
                    confirmed = await publisher.settle()
                self.assertEqual(await store.records(), (confirmed,))
                self.assertEqual(len(transport.calls), 1)
                claim.release()
            finally:
                release.set()
                await store.close()

    async def test_preflight_cancellation_does_not_attach_old_failure_or_retire_foreign_io(self):
        with tempfile.TemporaryDirectory() as directory:
            store, terminal = await retained_failure(directory, scope())
            transport = Transport()
            client = api(transport, scope().api_origin)
            client.last_failure = ApiFailure("earlier operation", "earlier failure")
            other_owner = object()
            transport.pending_owner = other_owner
            transport.retirement_confirmed = False
            claim = AttemptClaims().claim(scope(), ATTEMPT_REF)
            publisher = TerminalPublisher(client, store, claim)
            reached, release = threading.Event(), threading.Event()
            original = os.read

            def delayed_read(*args):
                reached.set()
                if not release.wait(2):
                    raise TimeoutError("test read was not released")
                return original(*args)

            try:
                with patch("provider_core.attempt_files.os.read", side_effect=delayed_read):
                    task = asyncio.create_task(publisher.publish(terminal))
                    self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIsNone(publisher.last_failure.api_failure)
                    self.assertEqual(transport.calls, [])
                    release.set()
                    with patch.object(transport, "retire") as retire:
                        self.assertIsNone(await publisher.settle())
                        retire.assert_not_awaited()
                    self.assertIs(transport.pending_owner, other_owner)
                    self.assertFalse(transport.retirement_confirmed)
                claim.release()
            finally:
                release.set()
                await store.close()

    async def test_publication_cannot_settle_another_callers_storage_result(self):
        with tempfile.TemporaryDirectory() as directory:
            store, terminal = await retained_failure(directory, scope())
            claim = AttemptClaims().claim(scope(), ATTEMPT_REF)
            transport = Transport()
            publisher = TerminalPublisher(api(transport, scope().api_origin), store, claim)
            reached, release = threading.Event(), threading.Event()
            original = os.read
            other_owner = object()

            def delayed_read(*args):
                reached.set()
                if not release.wait(2):
                    raise TimeoutError("test read was not released")
                return original(*args)

            try:
                with patch("provider_core.attempt_files.os.read", side_effect=delayed_read):
                    task = asyncio.create_task(store.records(owner=other_owner))
                    self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    with self.assertRaises(PublicationFailed):
                        await publisher.publish(terminal)
                    with self.assertRaises(PublicationRefused):
                        await publisher.settle()
                    self.assertIs(store.pending_owner, other_owner)
                    release.set()
                    self.assertEqual((await store.settle(owner=other_owner)).value, (terminal,))
                self.assertEqual(transport.calls, [])
                claim.release()
            finally:
                release.set()
                await store.close()

    async def test_failed_network_retirement_keeps_primary_failure_and_claim(self):
        primary = ExchangeFailed("response lost", primary=OSError("private-primary"))
        cleanup = ExchangeFailed("retirement failed", primary=OSError("private-cleanup"))
        transport = Transport(primary)
        transport.retirement_confirmed = False
        with tempfile.TemporaryDirectory() as directory:
            store, terminal = await retained_failure(directory, scope())
            claim = AttemptClaims().claim(scope(), ATTEMPT_REF)
            publisher = TerminalPublisher(api(transport, scope().api_origin), store, claim)
            try:
                with self.assertRaises(PublicationFailed) as failed:
                    await publisher.publish(terminal)
                with patch.object(transport, "retire", side_effect=cleanup), self.assertRaises(PublicationFailed) as settlement:
                    await publisher.settle()
                self.assertIs(settlement.exception.cause, failed.exception.cause)
                self.assertEqual(settlement.exception.settlement_failures, (cleanup,))
                cancellation = asyncio.CancelledError()
                with patch.object(transport, "retire", side_effect=cancellation), self.assertRaises(asyncio.CancelledError):
                    await publisher.settle()
                self.assertIs(publisher.last_failure.cause, failed.exception.cause)
                self.assertEqual(publisher.last_failure.settlement_failures, (cleanup, cancellation))
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                await publisher.settle()
                claim.release()
                self.assertEqual(len(transport.calls), 1)
            finally:
                await store.close()
