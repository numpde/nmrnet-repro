"""Interrupted explicit replay retains the original outcome and blocks fresh work."""

import asyncio
import unittest
from contextlib import suppress

from tests.integration.nmrnet.service_ports import SuccessiveServicePeers

from nmrnet_provider.admission import AdmissionHeld
from nmrnet_provider.service import ServiceFailed
from provider_core.attempt_record import TerminalConfirmed, TerminalPending
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.recovery import AttemptRecovery


class InterruptedServiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_terminal_recovery_keeps_the_original_request_without_another_job_or_automatic_retry(self):
        async with SuccessiveServicePeers() as fixture:
            service = fixture.service()
            fixture.stack.push_async_callback(service.settle)
            with self.assertRaises(ServiceFailed):
                await asyncio.wait_for(service.run(fixture.stop), 5)
            await service.settle()
            await fixture.reopen_store()
            records = await fixture.store.records()
            (pending,) = [record for record in records if type(record) is TerminalPending]
            (confirmed,) = [record for record in records if type(record) is TerminalConfirmed]
            claims = AttemptClaims().claim_records(records)
            pending_claim = claims[records.index(pending)]
            recovery = AttemptRecovery(fixture.api, fixture.store, pending_claim)
            fixture.recovery_release = asyncio.Event()
            task = asyncio.create_task(recovery.recover(pending))
            try:
                await asyncio.wait_for(fixture.recovery_entered.wait(), 2)
                with self.assertRaises(OwnershipRefused):
                    pending_claim.release()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                if pending_claim.operation_owner is not None:
                    await recovery.settle()
                self.assertEqual(await fixture.store.records(), records)
                self.assertEqual(fixture.complete_bodies, [confirmed.pending.body, pending.body, pending.body])
                self.assertEqual(len(fixture.allocations), 2)
                self.assertEqual(fixture.model_jobs, [job.job_ref for job in fixture.jobs[:2]])
                fixture.require_retired_science(2)
                requests = len(fixture.api_peer.requests)
                replacement = fixture.service()
                fixture.stack.push_async_callback(replacement.settle)
                with self.assertRaises(ServiceFailed) as caught:
                    await replacement.run(asyncio.Event())
                self.assertIsInstance(caught.exception.cause, AdmissionHeld)
                await replacement.settle()
                self.assertEqual(len(fixture.api_peer.requests), requests)
                self.assertEqual(len(fixture.allocations), 2)
                self.assertEqual(await fixture.store.records(), records)
            finally:
                fixture.recovery_release.set()
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                if pending_claim.operation_owner is not None:
                    await recovery.settle()
                for claim in claims:
                    claim.release()
