"""NMRNet must not START another Job while its prose service requests a wait.

Use the existing real ProviderService, SDK AttemptController, signed local TLS
Jobs API, durable result outbox and recording scientific workers. The delay
owner is a controlled stand-in for SDK ProseService: SDK B4 separately tests
how real HTTP 503 Retry-After reaches that owner's timing state.
"""
import asyncio
import unittest

from tests.integration.nmrnet.service_ports import ServicePeers, SuccessiveServicePeers


class NmrnetProseServiceOutageTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_job_waits_after_the_first_and_all_three_resume_without_operator_action(self):
        loop = asyncio.get_running_loop()
        delay_installed = asyncio.Event()
        delay_until = None
        async with SuccessiveServicePeers(lose_second_completion=False) as fixture:
            def retry_seconds():
                nonlocal delay_until
                if (len(fixture.allocations) == 1
                        and fixture.allocations[0].host.confirmed):
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
                    1, len(fixture.start_bodies),
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
