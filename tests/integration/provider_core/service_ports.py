"""Allocate recording science inside the trusted factory, after service admission.

This models the factory's setup/cleanup contract, not engine provisioning. Actual
NMRNet assembly and installed core run against the existing local TLS peers.
"""

import asyncio
import base64
import hashlib
import json
from builtins import BaseExceptionGroup
from dataclasses import asdict, replace
from unittest.mock import patch
from urllib.parse import urlsplit

from tests.fakes.attempts import complete_response, observation, progress_response, start_response
from tests.fakes.hello import receipt
from tests.fakes.job_feed import page
from tests.fakes.job_input import facts, response
from tests.integration.nmrnet.admission_ports import AdmissionPeers
from tests.integration.nmrnet.runtime_ports import require_api_credentials
from tests.integration.nmrnet.smiles_ports import InputPorts, SessionPorts
from tests.fakes.api_tls import verify_signed_request

from nmrnet_provider.admission import AdmissionPolicy
from nmrnet_provider.config import ANALYSIS_KIND_REF
from nmrnet_provider.runtime import assemble_attempt
from nmrnet_provider.service import Announcement, ProviderService, ServiceFailed, ServicePolicy
from provider_core.attempt_record import TerminalOperation
from provider_core.hello import AnalysisOffering
from provider_core.job_input import verify_job_input


class ServicePeers(AdmissionPeers):
    def __init__(self, *, maximum_live_attempts=2):
        super().__init__(InputPorts(), maximum_live_attempts=maximum_live_attempts)
        self.started_jobs = set()
        self.events = []
        self.api_notices = []
        self.allocations = []
        self.hellos = []
        self.hello_received = asyncio.Event()
        self.stop = asyncio.Event()
        self.attempt_stopping = asyncio.Event()
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

    def service(self, *, drain_seconds=0.1, hello_seconds=30, **changes):
        # Only announcement tests opt into fast periodic ticks. Unrelated
        # lifecycle proofs must not race a new Hello handshake during shutdown.
        policy = ServicePolicy(
            admission=AdmissionPolicy(2, 3),
            idle_seconds=0.05,
            hello_seconds=hello_seconds,
            drain_seconds=drain_seconds,
            settle_seconds=0.2,
            api_backoff_max_seconds=0.2,
        )
        arguments = {
            "config": self.config,
            "api": self.api,
            "open_attempt": self.open_attempt,
            "claims": self.claims,
            "current_announcement": lambda: self.snapshot,
            "policy": policy,
            "on_started": lambda: self.events.append("started"),
            "on_api_retry": lambda kind, failure, seconds: self.api_notices.append(("retry", kind, failure, seconds)),
            "on_api_recovered": lambda kind, failure: self.api_notices.append(("recovered", kind, failure)),
            "on_observed_crash": lambda crash: self.api_notices.append(("worker_crash", crash)),
            "factory_ready": lambda: True,
            "on_source_exhausted": lambda failure, pending: self.api_notices.append(("source_exhausted", failure, pending)),
        }
        return ProviderService(**(arguments | changes))

    async def settle_service(self, service):
        try:
            await service.settle()
        except ServiceFailed as failure:
            self.api_notices.append(("settlement", failure))
            if not service.shutdown_ready:
                raise

    def arguments(self, pending):
        return super().arguments(pending) | {"stopping": self.attempt_stopping}

    async def open_attempt(self, pending, *, stopping):
        self.attempt_stopping = stopping
        self.events.append("allocate")
        ports = self.scientific_ports(pending)
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
            controller = await assemble_attempt(**self.arguments(pending))
            ports.session = controller.session
            prepare = ports.session.prepare

            async def observe_preparation(*args, **kwargs):
                outcome = await prepare(*args, **kwargs)
                # Observe the actual session return in memory, never attach
                # private evidence to the relay's public DeliveryOutcome.
                ports.preparation_outcome = outcome
                return outcome

            ports.session.prepare = observe_preparation
            return controller
        except BaseException as cause:
            failures = [cause]
            try:
                if self.factory_cleanup_failure is not None:
                    # Assembly failed before prediction acquisition. Inject the
                    # independent failure into the input owner that exists.
                    with patch.object(ports.preparation, "retire", side_effect=self.factory_cleanup_failure):
                        await ports.session.retire()
                else:
                    await ports.session.retire()
            except (Exception, asyncio.CancelledError) as cleanup:
                failures.append(cleanup)
            if len(failures) > 1:
                raise BaseExceptionGroup("Recording factory setup and cleanup failed", failures) from None
            raise

    def observation_retry(self, attempt, failure, seconds):
        self.api_notices.append(("retry", "observation", failure, seconds, attempt.admission))

    def observation_recovered(self, attempt, failure):
        self.api_notices.append(("recovered", "observation", failure, attempt.admission))

    def scientific_ports(self, _pending):
        return SessionPorts()

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
        if path == "/provider/v1/jobs":
            self.events.append("discover")
            self.discovery_entered.set()
            if self.discovery_release is not None:
                await self.discovery_release.wait()
            if self.feed_replies is None and self.ports.instructions.job_ref in self.started_jobs:
                self.request_paths.append(path)
                return json.dumps(page() | {"analysis_kind_ref": ANALYSIS_KIND_REF, "jobs": [], "next_cursor": None}).encode()
        elif path.endswith("/start"):
            self.events.append("start")
        result = await super().api_reply(request, body)
        if path.endswith("/start"):
            accepted = json.loads(result)
            self.started_jobs.add(accepted["job_ref"])
        if path.endswith("/complete"):
            self.completed.set()
            if self.stop_after_complete:
                self.stop.set()
            if self.lose_completion:
                return None
        return result


class SuccessiveServicePeers(ServicePeers):
    """Three fixed Jobs; accept Job 2's outcome but lose its first acknowledgement.

    Real service, live ownership, delivery and signed local TLS are exercised.
    The generator and predictor remain recording ports, not native science.
    """

    def __init__(self, *, maximum_live_attempts=4, lose_second_completion=True):
        super().__init__(maximum_live_attempts=maximum_live_attempts)
        self.lose_second_completion = lose_second_completion
        self.stop_after_complete = False
        self.jobs = tuple(
            verify_job_input(
                response(raw, job_ref=f"job:successive-{index}"),
                feed=replace(facts(raw), job_ref=f"job:successive-{index}"),
            )
            for index, raw in enumerate(
                (
                    b"Job A: SMILES CCl; generate 2 conformers.",
                    b"Job B: SMILES CCl; generate 2 conformers.",
                    b"Job C: SMILES CCl; generate 2 conformers.",
                ),
                1,
            )
        )
        self.attempt_refs = tuple("execution_attempt:sha256:" + digit * 64 for digit in "123")
        self.start_bodies = []
        self.complete_bodies = []
        self.accepted_outcomes = {}
        self.model_jobs = []
        self.recovery_entered = asyncio.Event()
        self.recovery_release = None

    def scientific_ports(self, pending):
        self.require_retired_science(len(self.allocations))
        index = next(index for index, job in enumerate(self.jobs) if job.identity == pending.input)
        if index != len(self.allocations):
            raise AssertionError("Factory repeated or reordered a Job's scientific allocation")
        ports = SessionPorts()
        ports.instructions = self.jobs[index]
        ports.uploads.job_ref = ports.instructions.job_ref
        return ports

    def require_retired_science(self, count):
        if len(self.allocations) != count:
            raise AssertionError("Unexpected scientific allocation count")
        for ports in self.allocations:
            self.require_retired_job(ports)

    def require_retired_job(self, ports):
        if ports.worker.events != ["prepare", "execute", "close", "closed"] or not ports.host.confirmed:
            raise AssertionError("Prior prediction resources are not retired exactly once")
        ports.generator.generate.assert_awaited_once_with(b"CCl", 2)
        ports.allocate_generator.assert_awaited_once()
        ports.generator_host.retire.assert_awaited_once()
        ports.generator.close.assert_called_once()
        ports.generator.wait_closed.assert_awaited_once()

    def failure_receipt(self, _index, _value, _pending):
        raise AssertionError("This successive-Job fixture expects successful science")

    async def model_reply(self, request, raw):
        messages = json.loads(raw)["messages"]
        users = [message["content"] for message in messages if message["role"] == "user"]
        if users != [self.ports.instructions.specification_text]:
            raise AssertionError("Interpreter reused another Job's input or conversation")
        self.model_jobs.append(self.ports.instructions.job_ref)
        self.output = {"verbosity": ("low", "medium", "max")[self.jobs.index(self.ports.instructions)]}
        return await super().model_reply(request, raw)

    async def api_reply(self, request, body):
        require_api_credentials(request)
        verify_signed_request(request, body, origin=self.api_peer.origin, key=self.config.api.identity.private_key)
        method, target, _version = request.split(b"\r\n", 1)[0].decode().split(" ")
        path = urlsplit(target).path
        if path == "/provider/v1/hello":
            return await super().api_reply(request, body)
        if path == "/provider/v1/execution-attempts":
            raise AssertionError("Live admission must not read prior Attempt history")
        if path == "/provider/v1/jobs":
            available = self.jobs[len(self.start_bodies) : len(self.start_bodies) + 1]
            return json.dumps(
                page()
                | {
                    "analysis_kind_ref": ANALYSIS_KIND_REF,
                    "next_cursor": None,
                    "jobs": [
                        {
                            **asdict(job.identity),
                            "analysis_kind_ref": ANALYSIS_KIND_REF,
                            "created_at": "2026-10-18T00:00:00.123456Z",
                        }
                        for job in available
                    ],
                }
            ).encode()
        if path == "/provider/v1/execution-attempts/start":
            index = len(self.start_bodies)
            if index >= len(self.jobs) or json.loads(body)["job_ref"] != self.jobs[index].job_ref:
                raise AssertionError("A started Job was redispatched or eligibility order changed")
            if not self.claims.owns_job(self.config.lifecycle.scope, self.jobs[index].job_ref):
                raise AssertionError("START preceded its actual live Job reservation")
            if len(self.claims.live_owners()) != 1:
                raise AssertionError("A successive Job started before prior live ownership was released")
            self.start_bodies.append(body)
            return start_response(
                execution_attempt_ref=self.attempt_refs[index],
                job_ref=self.jobs[index].job_ref,
                analysis_kind_ref=ANALYSIS_KIND_REF,
            )
        for index, job in enumerate(self.jobs):
            reference = self.attempt_refs[index]
            if path == "/provider/v1/jobs/" + job.job_ref + "/input":
                return response(job.specification_bytes, job_ref=job.job_ref)
            if path == "/provider/v1/execution-attempts/" + reference:
                return observation(execution_attempt_ref=reference, job_ref=job.job_ref)
            if path == "/provider/v1/execution-attempts/" + reference + "/progress":
                return progress_response(execution_attempt_ref=reference, phase=json.loads(body)["phase"])
        if path not in ("/provider/v1/execution-attempts/complete", "/provider/v1/execution-attempts/fail") or method != "POST":
            raise AssertionError("Unexpected successive-service API request")
        value = json.loads(body)
        index = self.attempt_refs.index(value["execution_attempt_ref"])
        pending = self.relay.queue.next_pending(self.config.api.identity.origin, self.config.lifecycle.scope.provider_ref)
        if pending is None or pending.body != body or pending.job_ref != self.jobs[index].job_ref:
            raise AssertionError("API publication did not use the exact durable relay item for this Job")
        if path.endswith("/fail"):
            if pending.operation is not TerminalOperation.FAIL:
                raise AssertionError("Failure request differs from the retained terminal choice")
            result = self.failure_receipt(index, value, pending)
            return result
        if pending.operation is not TerminalOperation.COMPLETE:
            raise AssertionError("Completion request differs from the retained terminal choice")
        content = base64.b64decode(value["canonical_result_base64"], validate=True)
        public = json.loads(content)
        if (public["job_ref"], public["input_fingerprint"], public["execution_attempt_ref"]) != (
            self.jobs[index].job_ref,
            self.jobs[index].identity.input_fingerprint,
            self.attempt_refs[index],
        ):
            raise AssertionError("Scientific result reused another Job's source or Attempt identity")
        selection = public["generation"]["selection"]
        if (selection["job_ref"], selection["input_fingerprint"]) != (
            self.jobs[index].job_ref,
            self.jobs[index].identity.input_fingerprint,
        ):
            raise AssertionError("Generated source selection does not belong to the current Job")
        replayed = index in self.accepted_outcomes
        if replayed and self.accepted_outcomes[index] != body:
            raise AssertionError("Recovery changed the accepted terminal bytes")
        self.complete_bodies.append(body)
        self.accepted_outcomes[index] = body
        if index == 1 and not replayed and self.lose_second_completion:
            return None
        if index == 1 and self.recovery_release is not None:
            self.recovery_entered.set()
            await self.recovery_release.wait()
        if index == 2:
            self.stop.set()
        return complete_response(
            execution_attempt_ref=self.attempt_refs[index],
            analysis_result_ref="analysis_result:sha256:" + str(index + 1) * 64,
            result_schema_id="nmrnet.coordinate_shifts.result.v2",
            result_fingerprint="sha256:" + hashlib.sha256(content).hexdigest(),
            result_byte_length=len(content),
            replayed=replayed,
        )
