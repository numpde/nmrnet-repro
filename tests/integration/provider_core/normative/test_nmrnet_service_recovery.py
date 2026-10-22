"""Successive Jobs and explicit durable replay share the existing service owners."""

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.integration.nmrnet.budget_service import InputCallLimitPeers
from tests.integration.nmrnet.service_ports import SuccessiveServicePeers

from nmrnet_provider.admission import AdmissionHeld
from nmrnet_provider.config import load_recovery_config, load_recovery_inspection
from nmrnet_provider.process_reports import failure_evidence
from nmrnet_provider.recovery import inspect_recovery, recover_record
from nmrnet_provider.service import ServiceFailed
from provider_core.attempt_files import history_directory_name, record_name
from provider_core.attempt_record import TerminalConfirmed, TerminalOperation, TerminalPending
from provider_core.attempt_store import AttemptStore
from provider_core.controller import ControllerFailed, TerminalRecovery
from provider_core.publication import PublicationFailed


class SuccessiveServiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_limited_first_job_fails_then_later_jobs_finish_without_repeating_science(self):
        async with InputCallLimitPeers() as fixture:
            original_arguments = fixture.arguments
            retry, recovered = Mock(), Mock()
            policy = TerminalRecovery(2, 0.01, retry, recovered)

            def recovery_arguments(pending):
                return original_arguments(pending) | {"terminal_recovery": policy}

            service = fixture.service()
            fixture.stack.push_async_callback(service.settle)
            with patch.object(fixture, "arguments", side_effect=recovery_arguments):
                try:
                    await asyncio.wait_for(service.run(fixture.stop), 5)
                except ServiceFailed as cause:
                    raise AssertionError(json.dumps(failure_evidence(cause, secrets=("PRIVATE_MODEL_KEY",)))) from cause
            await service.settle()
            final = await fixture.retained_records()
            self.assertEqual(len(final), 3)
            self.assertTrue(all(type(record) is TerminalConfirmed for record in final))
            by_job = {record.pending.attempt.start.input.job_ref: record for record in final}
            first = by_job[fixture.jobs[0].job_ref]
            self.assertIs(first.pending.operation, TerminalOperation.FAIL)
            self.assertEqual(first.receipt.failure_code, "input_worker_call_limit")
            self.assertEqual(fixture.failure_bodies, [first.pending.body])
            self.assertTrue(all(by_job[job.job_ref].pending.operation is TerminalOperation.COMPLETE for job in fixture.jobs[1:]))
            self.assertEqual(fixture.model_jobs, [fixture.jobs[index].job_ref for index in (0, 0, 1, 2)])
            self.assertEqual(len(fixture.start_bodies), 3)
            self.assertEqual(len(fixture.complete_bodies), 3)
            fixture.require_retired_science(3)
            retry.assert_called_once()
            recovered.assert_called_once()
            self.assertEqual(recovered.call_args.args[0], by_job[fixture.jobs[1].job_ref].pending.attempt)
            await fixture.reopen_store()
            self.assertEqual(await fixture.retained_records(), final)

    async def test_three_jobs_share_one_active_slot_and_recover_lost_receipt_without_repeating_science(self):
        async with SuccessiveServicePeers(maximum_records=1) as fixture:
            notices = []
            policy = TerminalRecovery(
                2,
                0.01,
                lambda attempt, failure, seconds: notices.append(("retry", attempt, failure, seconds)),
                lambda attempt, failure: notices.append(("recovered", attempt, failure)),
            )
            original_arguments = fixture.arguments

            def recovery_arguments(pending):
                return original_arguments(pending) | {"terminal_recovery": policy}

            service = fixture.service()
            fixture.stack.push_async_callback(service.settle)
            with patch.object(fixture, "arguments", side_effect=recovery_arguments):
                await asyncio.wait_for(service.run(fixture.stop), 5)
            await service.settle()
            final = await fixture.retained_records()
            self.assertEqual(len(final), 3)
            self.assertEqual(len(await fixture.store.records()), 1)
            history = fixture.config.lifecycle.store_root / history_directory_name(fixture.store.scope)
            self.assertEqual(len(list(history.iterdir())), 2)
            self.assertTrue(all(type(record) is TerminalConfirmed for record in final))
            by_job = {record.pending.attempt.start.input.job_ref: record for record in final}
            second = by_job[fixture.jobs[1].job_ref]
            self.assertTrue(second.receipt.replayed)
            self.assertEqual(fixture.model_jobs, [job.job_ref for job in fixture.jobs])
            self.assertEqual(len(fixture.allocations), 3)
            fixture.require_retired_science(3)
            self.assertEqual(len(fixture.start_bodies), 3)
            self.assertEqual(
                fixture.complete_bodies,
                [
                    by_job[fixture.jobs[0].job_ref].pending.body,
                    second.pending.body,
                    second.pending.body,
                    by_job[fixture.jobs[2].job_ref].pending.body,
                ],
            )
            self.assertEqual([notice[0] for notice in notices], ["retry", "recovered"])
            for notice in notices:
                self.assertEqual(notice[1], second.pending.attempt)
                self.assertTrue(notice[2].attempt_mutation_may_have_committed)
            self.assertIs(notices[0][2], notices[1][2])
            await fixture.reopen_store()
            self.assertEqual(await fixture.retained_records(), final)

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
            records = await fixture.retained_records()
            self.assertEqual(len(records), 2)
            (confirmed,) = [record for record in records if type(record) is TerminalConfirmed]
            (pending,) = [record for record in records if type(record) is TerminalPending]
            self.assertEqual(await fixture.store.records(), (pending,))
            archived = fixture.config.lifecycle.store_root / history_directory_name(fixture.store.scope) / record_name(confirmed)
            archived_raw = archived.read_bytes()
            self.assertEqual(confirmed.pending.attempt.start.input, fixture.jobs[0].identity)
            self.assertEqual(pending.attempt.start.input, fixture.jobs[1].identity)
            body, evidence = pending.body, pending.evidence.content
            self.assertEqual(len(fixture.allocations), 2)
            self.assertEqual(fixture.model_jobs, [job.job_ref for job in fixture.jobs[:2]])
            fixture.require_retired_science(2)

            await fixture.reopen_store()
            self.assertEqual(await fixture.retained_records(), records)
            requests = len(fixture.api_peer.requests)
            blocked = fixture.service()
            fixture.stack.push_async_callback(blocked.settle)
            with self.assertRaises(ServiceFailed) as refused:
                await blocked.run(asyncio.Event())
            self.assertIsInstance(refused.exception.cause, AdmissionHeld)
            await blocked.settle()
            self.assertEqual(len(fixture.api_peer.requests), requests)
            self.assertEqual(len(fixture.allocations), 2)

            await fixture.store.close()
            diagnostics = Path(fixture.files.root) / "inspection"
            diagnostics.mkdir(mode=0o700)
            inspection = load_recovery_inspection(fixture.files.path)
            self.assertEqual(await inspect_recovery(inspection, diagnostics=diagnostics), 0)
            inspected = json.loads((diagnostics / "result.json").read_bytes())
            self.assertFalse(inspected["local_obligations_settled"])
            self.assertFalse(inspected["new_science_started"])
            self.assertFalse(inspected["requires_host_retirement"])
            self.assertEqual(len(fixture.api_peer.requests), requests)
            selection = {item["job_ref"]: item["record_sha256"] for item in inspected["records"]}
            self.assertEqual(set(selection), {fixture.jobs[1].job_ref})
            config = load_recovery_config(fixture.files.path)
            self.assertEqual(config.lifecycle, fixture.config.lifecycle)

            # Let the peer observe the actual production-created store, retaining
            # its durable-before-send check without replacing any store behavior.
            recovery_stores = []

            def observe_store(*arguments, **options):
                store = AttemptStore(*arguments, **options)
                fixture.store = store
                recovery_stores.append(store)
                return store

            with patch("nmrnet_provider.recovery.AttemptStore", side_effect=observe_store):
                diagnostics = Path(fixture.files.root) / "recovery-pending"
                diagnostics.mkdir(mode=0o700)
                self.assertEqual(
                    await recover_record(config, record_sha256=selection[fixture.jobs[1].job_ref], diagnostics=diagnostics), 0
                )
                report = json.loads((diagnostics / "result.json").read_bytes())
                self.assertEqual(report["state"], "stopped")
                self.assertFalse(report["requires_host_retirement"])
                self.assertFalse(report["new_science_started"])
                self.assertTrue(report["local_obligations_settled"])
                self.assertTrue(recovery_stores[-1].retirement_confirmed)
                self.assertEqual(len(fixture.api_peer.requests), requests + 1)
            self.assertEqual(len(recovery_stores), 1)
            self.assertEqual(archived.read_bytes(), archived_raw)
            await fixture.reopen_store()
            (recovered,) = [item for item in await fixture.store.records() if item.pending == pending]
            self.assertIs(type(recovered), TerminalConfirmed)
            self.assertTrue(recovered.receipt.replayed)
            self.assertEqual(recovered.pending.body, body)
            self.assertEqual(recovered.pending.evidence.content, evidence)
            self.assertEqual(fixture.complete_bodies, [confirmed.pending.body, body, body])
            self.assertEqual(len(fixture.allocations), 2)
            self.assertEqual(len(fixture.model_jobs), 2)

            fixture.stop = asyncio.Event()
            restarted = fixture.service()
            fixture.stack.push_async_callback(restarted.settle)
            await asyncio.wait_for(restarted.run(fixture.stop), 5)
            await restarted.settle()
            final = await fixture.retained_records()
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
            self.assertEqual(await fixture.retained_records(), final)
