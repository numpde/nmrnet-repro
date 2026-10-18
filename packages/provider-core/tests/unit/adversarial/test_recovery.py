"""Recovery reserves aliases without bypassing live owners or replaying stale state."""

import asyncio
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest.mock import patch

from provider_core.api import ApiFailure
from provider_core.attempt_record import acknowledge_start
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.recovery import AttemptRecovery, RecoveryFailed
from provider_core.transport import ExchangeFailed
from tests.fakes.api import Transport
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF
from tests.fakes.execution import acknowledged_store
from tests.fakes.publication import api


class RecoveryFailureTests(unittest.IsolatedAsyncioTestCase):
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
