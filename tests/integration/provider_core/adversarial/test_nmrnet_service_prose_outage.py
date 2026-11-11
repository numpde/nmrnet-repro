"""NMRNet must not START another Job while its prose service requests a wait.

Use the existing real ProviderService, SDK AttemptController, signed local TLS
Jobs API, durable result outbox and recording scientific workers. A real HTTPS
503 exercises the captured SDK ProseService; controlled waits isolate the
between-Job and operator-stop cases.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock

from tests.integration.nmrnet.service_ports import ServicePeers, SuccessiveServicePeers

from provider_core.interpreter import InterpretationFailed, Interpreter


class NmrnetProseServiceOutageTests(unittest.IsolatedAsyncioTestCase):
    async def test_captured_http_service_postpones_admission_and_recovers_without_rebinding(self):
        loop = asyncio.get_running_loop()
        outage_started = []

        def unavailable_reply(*_arguments):
            outage_started.append(loop.time())
            return b'{"error":{"message":"unavailable"}}'

        async with SuccessiveServicePeers(lose_second_completion=False) as fixture:
            original_reply = fixture.model_peer.response_factory
            fixture.model_peer.response_status = 503
            fixture.model_peer.response_headers = ((b"Retry-After", b"2"),)
            fixture.model_peer.response_factory = AsyncMock(side_effect=unavailable_reply)
            dispatch = AsyncMock()
            interpreter = Interpreter(
                fixture.prose_services,
                timeout_seconds=2,
                context_bytes=min(service.context_bytes for service in fixture.prose_services),
                secrets=(),
            )
            try:
                with self.assertRaises(InterpretationFailed):
                    await interpreter.run(
                        [{"role": "user", "content": "Earlier Job"}],
                        [
                            {
                                "type": "function",
                                "function": {
                                    "name": "prepare",
                                    "parameters": {"type": "object", "properties": {}},
                                },
                            }
                        ],
                        dispatch,
                    )
            finally:
                await interpreter.retire()
            dispatch.assert_not_awaited()
            self.assertEqual(1, len(fixture.model_peer.requests))
            waiting = asyncio.Event()
            recovered = asyncio.Event()

            def retry_seconds():
                delay = min(service.retry_seconds for service in fixture.prose_services)
                if delay > 0:
                    waiting.set()
                elif not recovered.is_set():
                    self.assertGreaterEqual(loop.time() - outage_started[0], 2)
                    self.assertEqual((), fixture.claims.live_owners())
                    self.assertEqual([], fixture.start_bodies)
                    self.assertEqual([], fixture.allocations)
                    self.assertNotIn("discover", fixture.events)
                    recovered.set()
                return delay

            service = fixture.service(prose_retry_seconds=retry_seconds)
            fixture.stack.push_async_callback(fixture.settle_service, service)
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(waiting.wait(), 1)
                self.assertEqual([], fixture.start_bodies)
                self.assertEqual([], fixture.allocations)
                self.assertEqual((), fixture.claims.live_owners())
                self.assertNotIn("discover", fixture.events)
                fixture.model_peer.response_status = 200
                fixture.model_peer.response_headers = None
                fixture.model_peer.response_factory = original_reply
                await asyncio.wait_for(task, 12)
                self.assertTrue(recovered.is_set())
                await fixture.wait_published(3)
                self.assertEqual(3, len(fixture.start_bodies))
                self.assertEqual(3, len(fixture.complete_bodies))
                self.assertEqual([job.job_ref for job in fixture.jobs], fixture.model_jobs)
                self.assertEqual((), fixture.claims.live_owners())
                self.assertTrue(service.shutdown_ready)
                for allocation in fixture.allocations:
                    fixture.require_retired_job(allocation)
            finally:
                fixture.stop.set()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)

    async def test_second_job_waits_after_the_first_and_all_three_resume_without_operator_action(self):
        loop = asyncio.get_running_loop()
        delay_installed = asyncio.Event()
        delay_until = None
        async with SuccessiveServicePeers(lose_second_completion=False) as fixture:

            def retry_seconds():
                nonlocal delay_until
                if len(fixture.allocations) == 1 and fixture.allocations[0].host.confirmed:
                    if delay_until is None:
                        delay_until = loop.time() + 0.3
                        delay_installed.set()
                    return max(0.0, delay_until - loop.time())
                return 0.0

            service = fixture.service(prose_retry_seconds=retry_seconds)
            fixture.stack.push_async_callback(fixture.settle_service, service)
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(delay_installed.wait(), 4)
                self.assertEqual(1, len(fixture.start_bodies))
                await asyncio.sleep(0.08)
                self.assertEqual(
                    1,
                    len(fixture.start_bodies),
                    "The next NMRNet Job started while the prose interpreter service was delayed",
                )
                await asyncio.wait_for(task, 8)
                await fixture.wait_published(3)
                self.assertEqual(len(fixture.jobs), len(fixture.start_bodies))
                self.assertEqual(len(fixture.jobs), len(fixture.complete_bodies))
                self.assertEqual(len(fixture.jobs), len(fixture.model_jobs))
                self.assertEqual([job.job_ref for job in fixture.jobs], fixture.model_jobs)
                self.assertEqual((), fixture.claims.live_owners())
                self.assertTrue(service.shutdown_ready)
                for allocation in fixture.allocations:
                    fixture.require_retired_job(allocation)
            finally:
                fixture.stop.set()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)

    async def test_operator_stop_interrupts_the_prose_delay_without_allocating_a_scientific_worker(self):
        async with ServicePeers() as fixture:
            service = fixture.service(prose_retry_seconds=lambda: 600.0)
            fixture.stack.push_async_callback(fixture.settle_service, service)
            task = asyncio.create_task(service.run(fixture.stop))
            try:
                await asyncio.wait_for(fixture.hello_received.wait(), 2)
                await asyncio.sleep(0.04)
                self.assertEqual([], fixture.allocations)
                self.assertNotIn("discover", fixture.events)
                fixture.stop.set()
                await asyncio.wait_for(task, 2)
                self.assertTrue(service.shutdown_ready)
                self.assertEqual([], fixture.allocations)
            finally:
                fixture.stop.set()
                await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
