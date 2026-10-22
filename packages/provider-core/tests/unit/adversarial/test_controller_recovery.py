"""Recovery stops on refusal, cleanup, storage failure, expiry and interruption."""

import asyncio
import ssl
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpcore

from provider_core.attempt_record import TerminalConfirmed, TerminalPending
from provider_core.attempt_store import StoreFailed
from provider_core.controller import AttemptController, ControllerFailed, TerminalRecovery, TerminalRecoveryExpired
from provider_core.failure_projection import owned_details, owned_relationships
from provider_core.ownership import AttemptClaims
from provider_core.publication import PublicationFailed
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import record, scope
from tests.fakes.attempts import complete_response
from tests.fakes.controller import SingleRetirementSession, cancel_task, empty_store, lifecycle_replies
from tests.fakes.publication import api


class ControllerRecoveryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def run_failure(self, terminal_reply, *, recovery, change_store=None, expected_exception=ControllerFailed):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = SingleRetirementSession(store)
            transport = Transport(*lifecycle_replies()[:-1], terminal_reply)
            controller = AttemptController(
                api(transport, scope().api_origin),
                store,
                AttemptClaims(),
                session,
                allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                terminal_recovery=recovery,
            )
            if change_store is not None:
                change_store(store)
            try:
                with self.assertRaises(expected_exception) as caught:
                    await controller.run(record())
                self.assertEqual(len(transport.calls), 9)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertIsInstance((await store.records())[0], TerminalPending)
                return caught.exception, controller
            finally:
                await store.close()

    async def test_permanent_reply_or_unsettled_cleanup_never_retries(self):
        for response in (
            reply(b"denied", status=401),
            reply(b"invalid receipt"),
            ExchangeFailed("Cleanup failed", primary=httpcore.ReadTimeout(), cleanup_failures=(OSError(),)),
        ):
            with self.subTest(response=response):
                retry = Mock()
                failure, _ = await self.run_failure(response, recovery=TerminalRecovery(1, 0.01, retry, Mock()))
                self.assertIsInstance(failure.cause, PublicationFailed)
                retry.assert_not_called()

    async def test_expiry_keeps_original_send_evidence_and_does_not_begin_another_publication(self):
        retry = Mock()

        # The observer deliberately uses the entire retry window; no new send is permitted afterwards.
        def consume_window(*_):
            time.sleep(0.02)

        retry.side_effect = consume_window
        failure, _ = await self.run_failure(
            reply(b"maintenance", status=503), recovery=TerminalRecovery(0.01, 0.01, retry, Mock())
        )
        self.assertIsInstance(failure.cause, TerminalRecoveryExpired)
        self.assertIsInstance(failure.cause.cause, PublicationFailed)
        self.assertEqual(failure.cause.cause.api_failure.status, 503)
        self.assertEqual(dict(owned_relationships(failure.cause))["owned cause"], failure.cause.cause)
        self.assertEqual(owned_details(failure.cause)["window_seconds"], 0.01)
        self.assertEqual(owned_details(failure.cause)["job_ref"], failure.cause.attempt.admission.job_ref)
        self.assertEqual(
            owned_details(failure.cause)["execution_attempt_ref"], failure.cause.attempt.admission.execution_attempt_ref
        )

    async def test_receipt_storage_failure_never_replays_a_successful_send(self):
        storage_failure = StoreFailed("Cannot retain receipt")

        def fail_confirmation(store):
            replace = store.replace

            async def replace_or_fail(old, new, *, owner=None):
                if type(new) is TerminalConfirmed:
                    raise storage_failure
                return await replace(old, new, owner=owner)

            store.replace = replace_or_fail

        retry = Mock()
        failure, controller = await self.run_failure(
            reply(complete_response(result_schema_id="result.v1")),
            recovery=TerminalRecovery(1, 0.01, retry, Mock()),
            change_store=fail_confirmation,
        )
        self.assertIs(failure.cause.cause, storage_failure)
        self.assertIsInstance(failure.cause.outcome, TerminalConfirmed)
        retry.assert_not_called()
        self.assertIs(controller.operation.last_failure, failure.cause)

    async def test_cancelled_backoff_keeps_pending_publication_without_resending(self):
        cancellation = asyncio.CancelledError()
        with (
            patch("provider_core.controller.asyncio.sleep", new=AsyncMock(side_effect=cancellation)),
        ):
            failure, _ = await self.run_failure(
                reply(b"maintenance", status=503),
                recovery=TerminalRecovery(1, 0.01, Mock(), Mock()),
                expected_exception=asyncio.CancelledError,
            )
        self.assertIs(failure, cancellation)
        self.assertIsInstance(failure.__context__, PublicationFailed)

    async def test_original_retirement_and_late_evidence_control_replay(self):
        for late_evidence in ("certificate", "cleanup", "availability"):
            with self.subTest(late_evidence=late_evidence):
                await self.check_original_retirement(late_evidence)

    async def check_original_retirement(self, late_evidence):
        with tempfile.TemporaryDirectory() as directory:
            store = await empty_store(directory, maximum_records=1)
            session = SingleRetirementSession(store)
            lost = ExchangeFailed("Read timed out", primary=httpcore.ReadTimeout())
            transport = Transport(*lifecycle_replies()[:-1], lost)
            original_exchange, original_retire = transport.exchange, transport.retire
            retirement_started, release_retirement = asyncio.Event(), asyncio.Event()
            cleanup = ExchangeFailed("Retirement failed", primary=OSError())

            async def exchange(request, *, maximum_bytes, owner=None):
                try:
                    return await original_exchange(request, maximum_bytes=maximum_bytes, owner=owner)
                except ExchangeFailed:
                    transport.retirement_confirmed, transport.pending_owner = False, owner
                    raise

            async def retire(*, owner=None):
                retirement_started.set()
                await release_retirement.wait()
                if late_evidence == "cleanup":
                    raise cleanup
                if late_evidence == "certificate":
                    lost.primary = ssl.SSLCertVerificationError("PRIVATE_CERTIFICATE")
                await original_retire(owner=owner)

            transport.exchange, transport.retire = exchange, retire
            retry = Mock()
            controller = AttemptController(
                api(transport, scope().api_origin),
                store,
                AttemptClaims(),
                session,
                allowed_job_states=frozenset({"open"}),
                poll_seconds=1,
                task_settle_seconds=0.1,
                terminal_recovery=TerminalRecovery(0.01, 0.001, retry, Mock()),
            )
            task = asyncio.create_task(controller.run(record()))
            try:
                await asyncio.wait_for(retirement_started.wait(), 1)
                self.assertEqual(len(transport.calls), 9)
                self.assertEqual([event[0] for event in session.events], ["prepare", "execute", "retire"])
                self.assertFalse(task.done())
                # Even an expired retry window cannot replace an independent cleanup failure.
                await asyncio.sleep(0.02)
                release_retirement.set()
                with self.assertRaises(ControllerFailed) as caught:
                    await task
                failure = caught.exception.cause
                if late_evidence == "availability":
                    self.assertIsInstance(failure, TerminalRecoveryExpired)
                    self.assertEqual(owned_details(failure)["job_ref"], record().input.job_ref)
                    failure = failure.cause
                self.assertIsInstance(failure, PublicationFailed)
                self.assertIs(failure.api_failure.cause, lost)
                if late_evidence == "cleanup":
                    self.assertIn(cleanup, failure.settlement_failures)
                retry.assert_not_called()
                self.assertEqual(len(transport.calls), 9)
                self.assertIsInstance((await store.records())[0], TerminalPending)
            finally:
                release_retirement.set()
                await cancel_task(task)
                await store.close()

    async def test_policy_rejects_nonfinite_bounds_and_missing_observers(self):
        for value in (True, 0, -1, float("nan"), float("inf"), "1"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    TerminalRecovery(value, 1, Mock(), Mock())
                with self.assertRaises(ValueError):
                    TerminalRecovery(1, value, Mock(), Mock())
        with self.assertRaises(ValueError):
            TerminalRecovery(1, 1, None, Mock())
