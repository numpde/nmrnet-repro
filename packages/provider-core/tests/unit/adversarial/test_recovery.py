"""Recovery reserves aliases without bypassing live owners or replaying stale state."""

import asyncio
import errno
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from provider_core.api import ApiFailure
from provider_core.attempt_record import TerminalStateObserved, acknowledge_start
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.publication import TerminalRecovery
from provider_core.recovery import AttemptRecovery, RecoveryFailed
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF, observation
from tests.fakes.execution import acknowledged_store
from tests.fakes.publication import api, retained_failure, retained_read_required, terminal_refusal


class RecoveryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_terminal_backoff_keeps_pending_bytes_and_failure_without_resending(self):
        with tempfile.TemporaryDirectory() as directory:
            store, pending = await retained_failure(directory, scope())
            claim = AttemptClaims().claim_record(pending)
            transport = Transport(reply(b"maintenance", status=503))
            entered = asyncio.Event()
            recovery = AttemptRecovery(
                api(transport, scope().api_origin), store, claim,
                terminal_recovery=TerminalRecovery(10, lambda *_: entered.set(), Mock()),
            )
            task = asyncio.create_task(recovery.recover(pending))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(await store.records(), (pending,))
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(transport.calls[0][0].content, pending.body)
                self.assertEqual(recovery.last_failure.api_failure.status, 503)
                self.assertIsNone(claim.operation_owner)
                claim.release()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await store.close()

    async def test_bad_or_unbound_read_preserves_the_read_only_obligation(self):
        for response in (
            reply(b"{}", status=404),
            terminal_refusal(404, code="unreviewed"),
            reply(observation(job_ref="job:other")),
            reply(observation(state="private-canary")),
        ):
            with self.subTest(response=response.status), tempfile.TemporaryDirectory() as directory:
                store, required = await retained_read_required(directory, scope())
                claim = AttemptClaims().claim_record(required)
                transport = Transport(response)
                try:
                    with self.assertRaises(RecoveryFailed) as failure:
                        await AttemptRecovery(api(transport, scope().api_origin), store, claim).recover(required)
                    self.assertIsNone(failure.exception.observed_record)
                    self.assertEqual(await store.records(), (required,))
                    self.assertEqual(len(transport.calls), 1)
                    self.assertEqual(transport.calls[0][0].method, "GET")
                    self.assertNotIn("private-canary", str(failure.exception))
                    claim.release()
                finally:
                    await store.close()

    async def test_cancelled_read_retires_original_owner_without_a_write_or_second_get(self):
        with tempfile.TemporaryDirectory() as directory:
            store, required = await retained_read_required(directory, scope())
            claim = AttemptClaims().claim_record(required)
            transport = Transport(asyncio.CancelledError())
            transport.retirement_confirmed = False
            recovery = AttemptRecovery(api(transport, scope().api_origin), store, claim)
            try:
                with patch.object(store._files, "replace", wraps=store._files.replace) as writes:
                    with self.assertRaises(asyncio.CancelledError):
                        await recovery.recover(required)
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    self.assertIsNone(await recovery.settle())
                    writes.assert_not_called()
                self.assertEqual(await store.records(), (required,))
                self.assertEqual(len(transport.calls), 1)
                claim.release()
            finally:
                await store.close()

    async def test_cancelled_readback_write_settles_the_same_write_before_claim_release(self):
        with tempfile.TemporaryDirectory() as directory:
            store, required = await retained_read_required(directory, scope())
            claim = AttemptClaims().claim_record(required)
            transport = Transport(reply(observation(state="succeeded")))
            recovery = AttemptRecovery(api(transport, scope().api_origin), store, claim)
            reached, release = threading.Event(), threading.Event()
            original = os.replace

            def delayed_replace(*args, **kwargs):
                reached.set()
                if not release.wait(3):
                    raise TimeoutError("Test readback write was not released")
                return original(*args, **kwargs)

            try:
                with patch("provider_core.attempt_files.os.replace", side_effect=delayed_replace) as writes:
                    task = asyncio.create_task(recovery.recover(required))
                    self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    retained = recovery.last_failure.observed_record
                    self.assertIsInstance(retained, TerminalStateObserved)
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    release.set()
                    self.assertEqual(await recovery.settle(), retained)
                    self.assertEqual(writes.call_count, 1)
                self.assertEqual(await store.records(), (retained,))
                self.assertEqual(len(transport.calls), 1)
                claim.release()
            finally:
                release.set()
                await store.close()

    async def test_failed_readback_write_keeps_observation_and_claim_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            store, required = await retained_read_required(directory, scope())
            claim = AttemptClaims().claim_record(required)
            transport = Transport(reply(observation(state="failed")))
            recovery = AttemptRecovery(api(transport, scope().api_origin), store, claim)
            try:
                with patch("provider_core.attempt_files.os.replace", side_effect=OSError(errno.EIO, "PRIVATE_PATH")) as writes:
                    with self.assertRaises(RecoveryFailed) as failure:
                        await recovery.recover(required)
                    observed = failure.exception.observed_record
                    self.assertIsInstance(observed, TerminalStateObserved)
                    with self.assertRaises(RecoveryFailed) as settlement:
                        await recovery.settle()
                    self.assertIs(settlement.exception.cause, failure.exception.cause)
                    self.assertEqual(settlement.exception.observed_record, observed)
                    self.assertEqual(writes.call_count, 1)
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                self.assertEqual(len(transport.calls), 1)
                self.assertIs(settlement.exception, failure.exception)
                self.assertEqual(settlement.exception.cleanup_failures, ())
                with self.assertRaises(RecoveryFailed) as repeated:
                    await recovery.settle()
                self.assertIs(repeated.exception, failure.exception)
            finally:
                await store.close()

    async def test_unexpected_storage_defect_preserves_its_cause_and_observed_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store, required = await retained_read_required(directory, scope())
            claim = AttemptClaims().claim_record(required)
            transport = Transport(reply(observation()))
            recovery = AttemptRecovery(api(transport, scope().api_origin), store, claim)
            defect = RuntimeError("PRIVATE_DEFECT")
            try:
                with patch.object(store, "replace", side_effect=defect) as writes:
                    with self.assertRaises(RecoveryFailed) as failure:
                        await recovery.recover(required)
                    self.assertIs(failure.exception.cause, defect)
                    self.assertIsInstance(failure.exception.observed_record, TerminalStateObserved)
                    with self.assertRaises(RecoveryFailed) as settlement:
                        await recovery.settle()
                    self.assertIs(settlement.exception, failure.exception)
                    writes.assert_awaited_once()
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                self.assertEqual(await store.records(), (required,))
                self.assertEqual(len(transport.calls), 1)
            finally:
                await store.close()

    async def test_remote_conflict_leaves_no_new_start_alias(self):
        claims = AttemptClaims()
        pending = record()
        retained = acknowledge_start(pending, admission())
        existing = claims.claim(scope(), ATTEMPT_REF)
        with self.assertRaises(OwnershipRefused):
            claims.claim_record(retained)
        claims.claim_start(pending).release()
        existing.require(scope(), ATTEMPT_REF)
        existing.release()

    async def test_duplicate_remote_across_inventory_reserves_nothing(self):
        claims = AttemptClaims()
        first = acknowledge_start(record(), admission())
        second = acknowledge_start(record("second"), admission())
        with self.assertRaises(OwnershipRefused):
            claims.claim_records((first, second))
        claims.claim_start(first.start).release()
        claims.claim_start(second.start).release()
        claims.claim(scope(), ATTEMPT_REF).release()

    async def test_late_inventory_conflict_preserves_existing_owner_and_reserves_nothing(self):
        claims = AttemptClaims()
        first, second = record(), record("second")
        existing = claims.claim_start(second)
        with self.assertRaises(OwnershipRefused):
            claims.claim_records((first, second))
        claims.claim_start(first).release()
        existing.require_start(second)
        existing.release()

    async def test_stale_acknowledgement_sends_nothing_and_retains_record(self):
        with tempfile.TemporaryDirectory() as directory:
            store, retained, original_claim = await acknowledged_store(directory)
            original_claim.release()
            stale = replace(retained, admission=replace(retained.admission, replayed=True))
            claim = AttemptClaims().claim_record(stale)
            transport = Transport()
            recovery = AttemptRecovery(api(transport, store.scope.api_origin), store, claim)
            try:
                with self.assertRaises(RecoveryFailed):
                    await recovery.recover(stale)
                self.assertEqual(transport.calls, [])
                self.assertEqual(await store.records(), (retained,))
                claim.release()
            finally:
                await store.close()

    async def test_cancelled_read_keeps_current_transport_evidence_until_settlement(self):
        with tempfile.TemporaryDirectory() as directory:
            store, retained, claim = await acknowledged_store(directory)
            transport = Transport(asyncio.CancelledError())
            transport.retirement_confirmed = False
            cause = ExchangeFailed("private transport retirement cause")
            transport.last_failure = cause
            client = api(transport, store.scope.api_origin)
            recovery = AttemptRecovery(client, store, claim)
            try:
                with self.assertRaises(asyncio.CancelledError):
                    await recovery.recover(retained)
                self.assertIs(recovery.last_failure.api_failure, client.last_failure)
                self.assertIs(recovery.last_failure.api_failure.cause, cause)
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                await recovery.settle()
                self.assertEqual(len(transport.calls), 1)
                self.assertIs(recovery.last_failure.api_failure.cause, cause)
                claim.release()
            finally:
                await store.close()

    async def test_cancelled_inventory_settles_original_read_without_stale_api_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            store, retained, claim = await acknowledged_store(directory)
            transport = Transport()
            client = api(transport, store.scope.api_origin)
            client.last_failure = ApiFailure("previous operation", "old evidence")
            recovery = AttemptRecovery(client, store, claim)
            reached, release = threading.Event(), threading.Event()
            original = store._files.records

            def delayed_records():
                reached.set()
                if not release.wait(2):
                    raise TimeoutError("test inventory was not released")
                return original()

            try:
                with patch.object(store._files, "records", side_effect=delayed_records) as reads:
                    task = asyncio.create_task(recovery.recover(retained))
                    self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIsNone(recovery.last_failure.api_failure)
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    release.set()
                    await recovery.settle()
                    self.assertEqual(reads.call_count, 1)
                self.assertEqual(transport.calls, [])
                claim.release()
            finally:
                release.set()
                await store.close()
