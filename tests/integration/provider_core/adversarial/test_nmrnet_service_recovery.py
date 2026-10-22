"""Interrupted explicit replay retains the original outcome and blocks fresh work."""

import asyncio
import threading
import unittest
from contextlib import suppress
from dataclasses import replace
from unittest.mock import AsyncMock, patch

from tests.integration.nmrnet.service_ports import ServicePeers, SuccessiveServicePeers

from nmrnet_provider.admission import AdmissionHeld
from nmrnet_provider.service import ServiceFailed
from provider_core.api import ProviderAPI
from provider_core.attempt_files import AttemptFiles, history_directory_name, record_name
from provider_core.attempt_record import TerminalConfirmed, TerminalPending
from provider_core.attempt_request import start_body
from provider_core.attempt_store import StoreRefused
from provider_core.ownership import AttemptClaims, OwnershipRefused
from provider_core.recovery import AttemptRecovery


class InterruptedServiceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_inventory_read_submits_no_new_archive(self):
        async with ServicePeers(maximum_records=1) as fixture:
            first = fixture.service()
            await first.run(fixture.stop)
            await first.settle()
            before = await fixture.store.records()
            (confirmed,) = before
            active = fixture.config.lifecycle.store_root / record_name(confirmed)
            raw = active.read_bytes()
            requests = tuple(fixture.api_peer.requests)
            entered, release = threading.Event(), threading.Event()
            original = AttemptFiles.records

            def held(files):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("Fixture archive inventory was not released")
                return original(files)

            stop = asyncio.Event()
            service = fixture.service()
            task = None
            try:
                with (
                    patch.object(AttemptFiles, "records", held),
                    patch.object(fixture.store, "archive_confirmed", new_callable=AsyncMock) as archive,
                ):
                    task = asyncio.create_task(service.run(stop))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    stop.set()
                    release.set()
                    await asyncio.wait_for(task, 1)
                    archive.assert_not_awaited()
                self.assertEqual(await fixture.store.records(), before)
                self.assertEqual(active.read_bytes(), raw)
                self.assertEqual(tuple(fixture.api_peer.requests), requests)
                self.assertEqual(len(fixture.allocations), 1)
            finally:
                release.set()
                stop.set()
                if task is not None:
                    await asyncio.gather(task, return_exceptions=True)
                await service.settle()

    async def test_mixed_unresolved_inventory_archives_nothing_and_refuses_fresh_work(self):
        async with ServicePeers(maximum_records=2) as fixture:
            first = fixture.service()
            await first.run(fixture.stop)
            await first.settle()
            (confirmed,) = await fixture.store.records()
            start = confirmed.pending.attempt.start
            pending = replace(start, body=start_body(start.input.job_ref, "unresolved:second"))
            await fixture.store.admit(pending)
            before = await fixture.store.records()
            raw = {path.name: path.read_bytes() for path in fixture.config.lifecycle.store_root.iterdir()}
            requests = tuple(fixture.api_peer.requests)
            fixture.stop = asyncio.Event()
            replacement = fixture.service()
            with self.assertRaises(ServiceFailed):
                await replacement.run(fixture.stop)
            await replacement.settle()
            self.assertEqual(await fixture.store.records(), before)
            self.assertEqual({path.name: path.read_bytes() for path in fixture.config.lifecycle.store_root.iterdir()}, raw)
            self.assertEqual(tuple(fixture.api_peer.requests), requests)
            self.assertEqual(len(fixture.allocations), 1)

    async def test_wrong_context_refuses_before_any_history_or_inventory_effect(self):
        async with ServicePeers() as fixture:
            first = fixture.service()
            await first.run(fixture.stop)
            await first.settle()
            before = await fixture.store.records()
            requests = tuple(fixture.api_peer.requests)
            for reason in ("identity", "topology", "scope"):
                with self.subTest(reason=reason):
                    identity = replace(fixture.config.api.identity) if reason == "identity" else fixture.config.api.identity
                    foreign = ProviderAPI(
                        identity,
                        fixture.config.api.http.new_transport(),
                        expected_topology="web" if reason == "topology" else fixture.config.api.expected_topology,
                    )
                    config = fixture.config
                    if reason == "scope":
                        config = replace(
                            config,
                            lifecycle=replace(
                                config.lifecycle, scope=replace(config.lifecycle.scope, generation_id="other-generation")
                            ),
                        )
                    service = fixture.service(config=config, api=foreign)
                    try:
                        with (
                            patch.object(fixture.store, "archive_confirmed", new_callable=AsyncMock) as archive,
                            patch.object(fixture.store, "records", new_callable=AsyncMock) as records,
                            self.assertRaises(ServiceFailed) as caught,
                        ):
                            await service.run(asyncio.Event())
                        self.assertIsInstance(caught.exception.cause, ValueError)
                        archive.assert_not_awaited()
                        records.assert_not_awaited()
                        await service.settle()
                    finally:
                        await foreign.retire()
            self.assertEqual(await fixture.store.records(), before)
            self.assertEqual(tuple(fixture.api_peer.requests), requests)
            self.assertEqual(len(fixture.allocations), 1)

    async def test_cancelled_archive_blocks_admission_until_its_original_store_future_settles(self):
        async with ServicePeers(maximum_records=1) as fixture:
            first = fixture.service()
            await first.run(fixture.stop)
            await first.settle()
            (confirmed,) = await fixture.store.records()
            active = fixture.config.lifecycle.store_root / record_name(confirmed)
            raw = active.read_bytes()
            requests = tuple(fixture.api_peer.requests)
            entered, release = threading.Event(), threading.Event()
            original = AttemptFiles._write_bytes
            writes = []

            def held(files, descriptor, content):
                writes.append(content)
                entered.set()
                if not release.wait(8):
                    raise TimeoutError("Fixture history write was not released")
                return original(files, descriptor, content)

            service = fixture.service()
            task = None
            with patch.object(AttemptFiles, "_write_bytes", held):
                try:
                    task = asyncio.create_task(service.run(asyncio.Event()))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 3)
                    self.assertIs(fixture.store.pending_owner, service)
                    self.assertIsNotNone(fixture.store.pending_operation)
                    with self.assertRaises(StoreRefused):
                        await fixture.store.close()
                    self.assertEqual(active.read_bytes(), raw)
                    self.assertEqual(tuple(fixture.api_peer.requests), requests)
                    self.assertEqual(len(fixture.allocations), 1)
                    release.set()
                    await service.settle()
                    self.assertIsNone(fixture.store.pending_operation)
                    self.assertEqual(writes, [raw])
                    self.assertEqual(await fixture.store.records(), ())
                    self.assertEqual(
                        (active.parent / history_directory_name(fixture.store.scope) / active.name).read_bytes(), raw
                    )
                finally:
                    release.set()
                    if task is not None:
                        await asyncio.gather(task, return_exceptions=True)
                    await service.settle()

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
            (confirmed,) = [record for record in await fixture.retained_records() if type(record) is TerminalConfirmed]
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
