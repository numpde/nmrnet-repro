"""Start interruption cannot skip retention, steal ownership or authorize science."""

import asyncio
import os
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from provider_core.api import ApiFailure
from provider_core.attempt_record import MAX_RECORD_BYTES, acknowledge_start
from provider_core.attempt_store import AttemptStore, StoreLimits
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.recovery import AttemptRecovery, RecoveryRefused
from provider_core.starting import AttemptStarter, StartFailed, StartRefused
from tests.fakes.api import Transport, reply
from tests.fakes.attempt_store import admission, record, scope
from tests.fakes.attempts import ATTEMPT_REF, start_response
from tests.fakes.publication import api


def store_at(directory):
    return AttemptStore(Path(directory), scope=scope(), limits=StoreLimits(1, MAX_RECORD_BYTES, 0), operation_seconds=1)


class StartingFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_acknowledgement_write_preserves_received_identity_and_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_at(directory)
            await store.open()
            pending = record()
            transport = Transport(reply(start_response()))
            claim = AttemptClaims().claim_start(pending)
            starter = AttemptStarter(api(transport, scope().api_origin), store, claim)
            try:
                with (
                    patch("provider_core.attempt_files.os.replace", side_effect=OSError("private-replacement-cause")),
                    self.assertRaises(StartFailed) as failed,
                ):
                    await starter.start(pending)
                self.assertEqual(failed.exception.acknowledgement, acknowledge_start(pending, admission()))
                self.assertNotIn("private-replacement-cause", str(failed.exception))
                claim.require(scope(), ATTEMPT_REF)
                with self.assertRaises(OwnershipRefused):
                    claim.release()
                with self.assertRaises(StartFailed) as unsettled:
                    await starter.settle()
                self.assertIs(unsettled.exception.cause, failed.exception.cause)
                self.assertEqual(len(transport.calls), 1)
            finally:
                await store.close()

    async def test_retention_failure_prevents_every_api_send(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_at(directory)
            await store.open()
            pending = record()
            transport = Transport()
            claim = AttemptClaims().claim_start(pending)
            starter = AttemptStarter(api(transport, scope().api_origin), store, claim)
            try:
                with (
                    patch("provider_core.attempt_files.os.fsync", side_effect=OSError("private-disk-cause")),
                    self.assertRaises(StartFailed) as failed,
                ):
                    await starter.start(pending)
                self.assertIsNone(failed.exception.acknowledgement)
                self.assertEqual(transport.calls, [])
                self.assertNotIn("private-disk-cause", str(failed.exception))
                claim.release()
            finally:
                await store.close()

    async def test_missing_advanced_and_foreign_generation_recovery_never_sends(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_at(directory)
            await store.open()
            pending = record()
            transport = Transport()
            claim = AttemptClaims().claim_start(pending)
            starter = AttemptStarter(api(transport, scope().api_origin), store, claim)
            try:
                with self.assertRaises(StartRefused):
                    await starter.resume(pending)
                wrong_scope = replace(pending, scope=replace(scope(), generation_id="another"))
                with self.assertRaises(StartRefused):
                    await starter.start(wrong_scope)
                await store.admit(pending)
                await store.replace(pending, acknowledge_start(pending, admission()))
                with self.assertRaises(StartRefused):
                    await starter.resume(pending)
                with self.assertRaises(StartFailed):
                    await starter.start(pending)
                self.assertEqual(transport.calls, [])
                claim.release()
            finally:
                await store.close()

    async def test_remote_conflict_retains_admission_and_both_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_at(directory)
            await store.open()
            pending = record()
            claims = AttemptClaims()
            existing = claims.claim(scope(), ATTEMPT_REF)
            starting = claims.claim_start(pending)
            transport = Transport(reply(start_response()))
            starter = AttemptStarter(api(transport, scope().api_origin), store, starting)
            try:
                with self.assertRaises(StartFailed) as failed:
                    await starter.start(pending)
                self.assertEqual(failed.exception.acknowledgement, acknowledge_start(pending, admission()))
                self.assertEqual(await store.records(), (pending,))
                existing.require(scope(), ATTEMPT_REF)
                with self.assertRaises(OwnershipRefused):
                    starting.release()
                with self.assertRaises(StartFailed):
                    await starter.settle()
                self.assertEqual(len(transport.calls), 1)
                existing.release()
            finally:
                await store.close()

    async def test_recovery_settles_original_acknowledgement_write_without_resend(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_at(directory)
            await store.open()
            pending = record()
            await store.admit(pending)
            transport = Transport(reply(start_response()))
            claim = AttemptClaims().claim_start(pending)
            recovery = AttemptRecovery(api(transport, scope().api_origin), store, claim)
            reached, release = threading.Event(), threading.Event()
            original = os.replace

            def delayed_replace(*args, **kwargs):
                reached.set()
                if not release.wait(2):
                    raise TimeoutError("test replacement was not released")
                return original(*args, **kwargs)

            try:
                with patch("provider_core.attempt_files.os.replace", side_effect=delayed_replace) as writes:
                    task = asyncio.create_task(recovery.recover(pending))
                    self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual(recovery.last_failure.acknowledgement, acknowledge_start(pending, admission()))
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    with self.assertRaises(RecoveryRefused):
                        await recovery.recover(pending)
                    release.set()
                    acknowledged = await recovery.settle()
                    self.assertEqual(writes.call_count, 1)
                self.assertEqual(await store.records(), (acknowledged,))
                claim.require(scope(), ATTEMPT_REF)
                self.assertEqual(len(transport.calls), 1)
                claim.release()
            finally:
                release.set()
                await store.close()

    async def test_cancelled_admission_settlement_never_sends_or_retires_foreign_api(self):
        with tempfile.TemporaryDirectory() as directory:
            store = store_at(directory)
            await store.open()
            pending = record()
            transport = Transport()
            client = api(transport, scope().api_origin)
            client.last_failure = ApiFailure("earlier operation", "earlier cause")
            other = object()
            transport.pending_owner = other
            transport.retirement_confirmed = False
            claim = AttemptClaims().claim_start(pending)
            starter = AttemptStarter(client, store, claim)
            reached, release = threading.Event(), threading.Event()
            original = os.fsync

            def delayed_sync(*args):
                reached.set()
                if not release.wait(2):
                    raise TimeoutError("test sync was not released")
                return original(*args)

            try:
                with patch("provider_core.attempt_files.os.fsync", side_effect=delayed_sync):
                    task = asyncio.create_task(starter.start(pending))
                    self.assertTrue(await asyncio.to_thread(reached.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertIsNone(starter.last_failure.api_failure)
                    with self.assertRaises(OwnershipRefused):
                        claim.release()
                    release.set()
                    with patch.object(transport, "retire") as retire:
                        self.assertIsNone(await starter.settle())
                        retire.assert_not_awaited()
                self.assertEqual(await store.records(), (pending,))
                self.assertEqual(transport.calls, [])
                self.assertIs(transport.pending_owner, other)
                claim.release()
            finally:
                release.set()
                await store.close()
