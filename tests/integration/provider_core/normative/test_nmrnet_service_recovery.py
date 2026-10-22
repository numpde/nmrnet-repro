"""Successive Jobs and explicit durable replay share the existing service owners."""

import asyncio
import json
import unittest

from tests.integration.nmrnet.service_ports import SuccessiveServicePeers

from nmrnet_provider.admission import AdmissionHeld
from nmrnet_provider.service import ServiceFailed
from provider_core.attempt_record import TerminalConfirmed, TerminalPending
from provider_core.controller import ControllerFailed
from provider_core.ownership import AttemptClaims
from provider_core.publication import PublicationFailed
from provider_core.recovery import AttemptRecovery


class SuccessiveServiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_jobs_then_exact_recovery_and_restart_for_a_third_without_repeating_science(self):
        async with SuccessiveServicePeers() as fixture:
            first = fixture.service()
            fixture.stack.push_async_callback(first.settle)
            with self.assertRaises(ServiceFailed) as lost:
                await asyncio.wait_for(first.run(fixture.stop), 5)
            (failure,) = [cause for operation, cause in lost.exception.failures if operation == "Attempt work"]
            self.assertIsInstance(failure, ControllerFailed)
            self.assertIsInstance(failure.cause, PublicationFailed)
            self.assertTrue(failure.cause.api_failure.attempt_mutation_may_have_committed)
            await first.settle()
            records = await fixture.store.records()
            self.assertEqual(len(records), 2)
            (confirmed,) = [record for record in records if type(record) is TerminalConfirmed]
            (pending,) = [record for record in records if type(record) is TerminalPending]
            self.assertEqual(confirmed.pending.attempt.start.input, fixture.jobs[0].identity)
            self.assertEqual(pending.attempt.start.input, fixture.jobs[1].identity)
            body, evidence = pending.body, pending.evidence.content
            self.assertEqual(len(fixture.allocations), 2)
            self.assertEqual(fixture.model_jobs, [job.job_ref for job in fixture.jobs[:2]])
            fixture.require_retired_science(2)

            await fixture.reopen_store()
            self.assertEqual(await fixture.store.records(), records)
            requests = len(fixture.api_peer.requests)
            blocked = fixture.service()
            fixture.stack.push_async_callback(blocked.settle)
            with self.assertRaises(ServiceFailed) as refused:
                await blocked.run(asyncio.Event())
            self.assertIsInstance(refused.exception.cause, AdmissionHeld)
            await blocked.settle()
            self.assertEqual(len(fixture.api_peer.requests), requests)
            self.assertEqual(len(fixture.allocations), 2)

            claims = AttemptClaims().claim_records(records)
            pending_claim = claims[records.index(pending)]
            confirmed_claim = claims[records.index(confirmed)]
            recovery = AttemptRecovery(fixture.api, fixture.store, pending_claim)
            try:
                self.assertEqual(await AttemptRecovery(fixture.api, fixture.store, confirmed_claim).recover(confirmed), confirmed)
                recovered = await recovery.recover(pending)
                self.assertIs(type(recovered), TerminalConfirmed)
                self.assertTrue(recovered.receipt.replayed)
                self.assertEqual(recovered.pending, pending)
                self.assertEqual(recovered.pending.body, body)
                self.assertEqual(recovered.pending.evidence.content, evidence)
            finally:
                if pending_claim.operation_owner is not None:
                    await recovery.settle()
                for claim in claims:
                    claim.release()
            self.assertEqual(fixture.complete_bodies, [confirmed.pending.body, body, body])
            self.assertEqual(len(fixture.allocations), 2)
            self.assertEqual(len(fixture.model_jobs), 2)

            fixture.stop = asyncio.Event()
            restarted = fixture.service()
            fixture.stack.push_async_callback(restarted.settle)
            await asyncio.wait_for(restarted.run(fixture.stop), 5)
            await restarted.settle()
            final = await fixture.store.records()
            self.assertEqual(len(final), 3)
            self.assertTrue(all(type(record) is TerminalConfirmed for record in final))
            by_job = {record.pending.attempt.start.input.job_ref: record for record in final}
            self.assertEqual(by_job[fixture.jobs[0].job_ref], confirmed)
            self.assertEqual(by_job[fixture.jobs[1].job_ref], recovered)
            third = by_job[fixture.jobs[2].job_ref]
            self.assertEqual(third.pending.attempt.start.input, fixture.jobs[2].identity)
            self.assertEqual(fixture.model_jobs, [job.job_ref for job in fixture.jobs])
            self.assertEqual(len(fixture.allocations), 3)
            fixture.require_retired_science(3)
            self.assertEqual(fixture.complete_bodies, [confirmed.pending.body, body, body, third.pending.body])
            starts = [json.loads(value) for value in fixture.start_bodies]
            self.assertEqual([item["job_ref"] for item in starts], [job.job_ref for job in fixture.jobs])
            self.assertEqual(len({item["provider_attempt_key"] for item in starts}), 3)
            self.assertEqual(len({record.pending.attempt.admission.execution_attempt_ref for record in final}), 3)
            self.assertEqual(by_job[fixture.jobs[1].job_ref].pending.body, body)
            self.assertEqual(by_job[fixture.jobs[1].job_ref].pending.evidence.content, evidence)
            await fixture.reopen_store()
            self.assertEqual(await fixture.store.records(), final)
