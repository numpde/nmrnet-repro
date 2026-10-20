"""Allocate recording science inside the trusted factory, after service admission.

This models the factory's setup/cleanup contract, not engine provisioning. Actual
NMRNet assembly and installed core run against the existing local TLS peers.
"""

import asyncio
import json
from builtins import BaseExceptionGroup
from urllib.parse import urlsplit

from tests.fakes.attempt_inventory import response as inventory_response
from tests.fakes.hello import receipt
from tests.integration.nmrnet.admission_ports import AdmissionPeers
from tests.integration.nmrnet.runtime_ports import require_api_credentials
from tests.integration.nmrnet.smiles_ports import InputPorts, SessionPorts

from nmrnet_provider.admission import AdmissionPolicy
from nmrnet_provider.runtime import assemble_attempt
from nmrnet_provider.service import Announcement, ProviderService, ServicePolicy
from provider_core.hello import AnalysisOffering


class ServicePeers(AdmissionPeers):
    def __init__(self, *, maximum_records=2):
        super().__init__(InputPorts(), maximum_records=maximum_records)
        self.inventory_replies = [inventory_response(attempts=[], next_cursor=None)] * 2
        self.events = []
        self.allocations = []
        self.hellos = []
        self.hello_received = asyncio.Event()
        self.stop = asyncio.Event()
        self.completed = asyncio.Event()
        self.factory_entered = asyncio.Event()
        self.discovery_entered = asyncio.Event()
        self.discovery_release = None
        self.factory_cleanup_failure = None
        self.factory_release = None
        self.resist_factory_cancel = False
        self.factory_cancelled = asyncio.Event()
        self.execution_entered = asyncio.Event()
        self.execution_release = None
        self.stop_after_complete = True
        self.lose_completion = False
        self.hello_failure_after = None
        self.snapshot = Announcement(
            "Fixture NMRNet", "Local coordinator proof", (AnalysisOffering("peaks_from_mol", "Authored test"),)
        )

    def service(self, *, drain_seconds=0.1, **changes):
        policy = ServicePolicy(
            admission=AdmissionPolicy(2, 3),
            idle_seconds=0.05,
            hello_seconds=0.05,
            drain_seconds=drain_seconds,
            settle_seconds=0.2,
        )
        arguments = {
            "config": self.config,
            "store": self.store,
            "api": self.api,
            "open_attempt": self.open_attempt,
            "current_announcement": lambda: self.snapshot,
            "policy": policy,
            "on_started": lambda: self.events.append("started"),
        }
        return ProviderService(**(arguments | changes))

    async def open_attempt(self, pending):
        self.events.append("allocate")
        ports = SessionPorts()
        ports.host.failure = self.factory_cleanup_failure
        self.allocations.append(ports)
        self.ports = ports
        original = ports.worker.execute

        async def execute(handle):
            self.execution_entered.set()
            if self.execution_release is not None:
                await self.execution_release.wait()
            return await original(handle)

        ports.worker.execute = execute
        self.factory_entered.set()
        try:
            if self.factory_release is not None:
                try:
                    await self.factory_release.wait()
                except asyncio.CancelledError:
                    self.factory_cancelled.set()
                    if not self.resist_factory_cancel:
                        raise
                    await self.factory_release.wait()
            return await assemble_attempt(**self.arguments(pending))
        except BaseException as cause:
            failures = [cause]
            # Assembly's read uses owner=None. Only this serialized factory
            # invocation owns that setup read; the service must not steal it.
            if self.store.pending_operation is not None:
                try:
                    await self.store.settle()
                except (Exception, asyncio.CancelledError) as cleanup:
                    failures.append(cleanup)
            try:
                await ports.session.retire()
            except (Exception, asyncio.CancelledError) as cleanup:
                failures.append(cleanup)
            if len(failures) > 1:
                raise BaseExceptionGroup("Recording factory setup and cleanup failed", failures) from None
            raise

    async def api_reply(self, request, body):
        require_api_credentials(request)
        path = urlsplit(request.split(b"\r\n", 1)[0].split(b" ")[1].decode()).path
        if path == "/provider/v1/hello":
            self.events.append("hello")
            self.hellos.append(json.loads(body))
            self.hello_received.set()
            if self.hello_failure_after is not None and len(self.hellos) > self.hello_failure_after:
                return b"PRIVATE invalid Hello receipt"
            return receipt()
        if path == "/provider/v1/execution-attempts":
            self.events.append("inventory")
        elif path == "/provider/v1/jobs":
            self.events.append("discover")
            self.discovery_entered.set()
            if self.discovery_release is not None:
                await self.discovery_release.wait()
        elif path.endswith("/start"):
            self.events.append("start")
        result = await super().api_reply(request, body)
        if path.endswith("/complete"):
            self.completed.set()
            if self.stop_after_complete:
                self.stop.set()
            if self.lose_completion:
                return None
        return result
