"""Publication failures retain exact effect evidence and cannot steal ownership."""

import asyncio
import json
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from provider_core.api import ApiFailure
from provider_core.attempt_record import TerminalConfirmed, TerminalHeld
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.publication import PublicationFailed, PublicationRefused, TerminalPublisher
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply, response_headers
from tests.fakes.attempt_store import scope
from tests.fakes.attempts import ATTEMPT_REF, fail_response
from tests.fakes.publication import api, retained_failure


class PublicationFailureTests(unittest.IsolatedAsyncioTestCase):
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
