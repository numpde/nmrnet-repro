"""Local signed discovery, source and model peers; science remains recording.

Reuses job-feed/signature fixtures from provider-core's test_job_discovery.py
and preparation ports from test_nmrnet_interpreter.py, at c452e23. These peers
exercise real HTTPS and the installed core, not a deployed API or external model.
"""

import asyncio
import base64
import hashlib
import json
import sys
import tempfile
import threading
from contextlib import AsyncExitStack
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import AsyncMock

from http_sf import parse
from tests.fakes.acquisition import store_headers
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempts import ATTEMPT_REF, complete_response, fail_response, observation, progress_response, start_response
from tests.fakes.job_feed import page
from tests.fakes.job_input import response
from tests.fakes.tls_peer import TlsPeer
from tests.fakes.uploads import UPLOAD_REF, encoded, grant, member, membership
from tests.integration.nmrnet.config_ports import ConfigFiles
from tests.integration.nmrnet.interpreter_ports import tool_reply

from nmrnet_provider.config import ANALYSIS_KIND_REF, load_provider_config
from provider_core.api import ProviderAPI
from provider_core.attempt_record import StartPending
from provider_core.attempt_request import start_body
from provider_core.lifetime import ProviderLifetime
from provider_core.controller import ControllerFailed, LiveStartPolicy
from provider_core.delivery_store import QueueLimits
from provider_core.provider_proxy import ServicePolicy, ServiceProxyServer
from provider_core.shared_delivery_ipc import SharedDeliverySocket
from provider_core.shared_delivery_service import SharedDeliveryService
from provider_core.execution import ObservationRecovery
from provider_core.ownership import AttemptClaims


class JoinedDeliveryProxy(ServiceProxyServer):
    daemon_threads = False


# Failed fixture owners retain their peers and temporary files for inspection.
HELD_FIXTURES = []


def require_api_credentials(request):
    if b"bearer " in request.lower() or b"PRIVATE_MODEL_KEY" in request:
        raise AssertionError("API received a source or model bearer")


def require_model_credentials(request):
    if b"authorization: bearer private_model_key\r\n" not in request.lower():
        raise AssertionError("Model did not receive only its configured key")
    if b"signature:" in request.lower():
        raise AssertionError("Model received API signing headers")


def require_download_credentials(request, body):
    if request.split(b"\r\n", 1)[0] != f"GET /upload/v1/uploads/{UPLOAD_REF}/bytes HTTP/1.1".encode() or body:
        raise AssertionError("Store received an unexpected Upload bytes operation")
    headers = [(name.lower(), value.strip()) for line in request.split(b"\r\n")[1:] if b":" in line
               for name, value in [line.split(b":", 1)]]
    if [value for name, value in headers if name == b"authorization"] != [b"Bearer " + grant()["capability"].encode()]:
        raise AssertionError("Store did not receive exactly its granted download bearer")
    if any(name in (b"signature", b"signature-input") for name, _ in headers) or b"private_model_key" in request.lower():
        raise AssertionError("Store received API signing headers or the model credential")


class RuntimePeers:
    def __init__(self, ports, route, *, maximum_live_attempts=2):
        self.ports, self.route = ports, route
        self.allocate_prediction = AsyncMock(side_effect=self._allocate_prediction)
        self.maximum_live_attempts = maximum_live_attempts
        self.stop = asyncio.Event()
        self.operator_notices = []
        self.progress_notices = []
        self.result = None
        self.delivery_notices = []
        # These lifecycle witnesses inspect the complete public audit record.
        self.output = {"verbosity": "max"}
        self.corrupt_input = False
        self.stack = AsyncExitStack()
        self.resources = AsyncExitStack()
        self.relay = self.relay_task = None
        self.proxy = self.proxy_thread = None

    async def __aenter__(self):
        try:
            self.api_peer = await self.resources.enter_async_context(api_tls_peer("success", response_factory=self.api_reply))
            self.model_peer = await self.resources.enter_async_context(TlsPeer("success", response_factory=self.model_reply))
            headers = tuple((name, value) for name, value in store_headers(self.ports.source) if name != b"content-length")
            self.store_peer = await self.resources.enter_async_context(
                TlsPeer("success", response_body=self.ports.source, response_headers=headers)
            )
            directory = self.resources.enter_context(tempfile.TemporaryDirectory())
            self.files = ConfigFiles(
                directory,
                api_origin=self.api_peer.origin,
                model_origin=self.model_peer.origin,
                store_origin=self.store_peer.origin,
                ca_file=Path(self.api_peer.scratch.name) / "cert.pem",
                model_ca_file=Path(self.model_peer.scratch.name) / "cert.pem",
                store_ca_file=Path(self.store_peer.scratch.name) / "cert.pem",
                maximum_live_attempts=self.maximum_live_attempts,
            )
            self.config = load_provider_config(self.files.path)
            self.files.state.mkdir(mode=0o700)
            self.lifetime = ProviderLifetime(self.config.lifecycle.lifetime_root).open()
            self.stack.callback(self.lifetime.close)
            self.claims = AttemptClaims.live(limits=self.config.lifecycle.live_limits)
            self.api = ProviderAPI(
                self.config.api.identity,
                self.config.api.http.new_transport(),
                expected_topology=self.config.api.expected_topology,
            )
            self.stack.push_async_callback(self.api.retire)
            relay_root = Path(directory) / "relay-state"
            relay_root.mkdir(mode=0o700)
            self.config.delivery.endpoint.socket_path.parent.mkdir(mode=0o710)
            policy = ServicePolicy.admit("local", [urlsplit(self.api_peer.origin).netloc], ["127.0.0.1=127.0.0.1"])
            self.proxy = JoinedDeliveryProxy(Path(directory) / "delivery-proxy.sock", "lo", policy, status_directory=Path(directory))
            self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, kwargs={"poll_interval": 0.01})
            self.proxy_thread.start()
            endpoint = self.config.delivery.endpoint
            self.relay = SharedDeliveryService(
                relay_root, limits=QueueLimits(8, 16777216, 8, 16777216),
                socket=SharedDeliverySocket(endpoint.socket_path, endpoint.socket_group_id),
                proxy_socket=Path(directory) / "delivery-proxy.sock", request_seconds=2,
                retry_initial_seconds=0.01, retry_max_seconds=0.1,
                on_notice=lambda error, facts: self.delivery_notices.append((error, facts)),
                diagnostic_status=lambda: {"output_failures": 0},
            )
            self.relay_task = asyncio.create_task(self.relay.run())
            await asyncio.wait_for(self.relay.ready.wait(), 2)
            return self
        except BaseException:
            await self.__aexit__(*sys.exc_info())
            raise

    async def __aexit__(self, *error):
        try:
            suppressed = await self.stack.__aexit__(*error)
        except BaseException:
            HELD_FIXTURES.append(self)
            raise
        await self.close_resources()
        return suppressed

    async def settle_controller(self, controller):
        try:
            await controller.settle()
        except ControllerFailed as failure:
            # A passive live exclusion can survive cleanup. Keep its diagnostic;
            # a pending report or physical operation must still fail teardown.
            self.operator_notices.append(("settlement", failure))
            if not controller.shutdown_ready:
                raise

    async def close_relay(self):
        self.relay.request_stop()
        try:
            await asyncio.wait_for(asyncio.shield(self.relay_task), 3)
        except BaseException:
            if self.relay_task.done():
                await self.relay.close()
            raise
        await self.relay.close()

    async def close_resources(self):
        relay_failure = None
        try:
            if self.relay_task is not None:
                try:
                    await self.close_relay()
                except BaseException as error:
                    if not self.relay.retirement_confirmed:
                        raise
                    relay_failure = error
                if not self.relay.retirement_confirmed:
                    raise AssertionError("Delivery relay retirement is unconfirmed; retain fixture resources.")
            if self.proxy is not None:
                if self.proxy_thread is not None and self.proxy_thread.ident is not None:
                    await self.close_proxy(self.proxy, self.proxy_thread)
                else:
                    self.proxy.server_close()
            await self.resources.aclose()
        except BaseException:
            HELD_FIXTURES.append(self)
            raise
        if relay_failure is not None:
            raise relay_failure

    async def close_proxy(self, proxy, thread):
        await asyncio.to_thread(proxy.shutdown)
        await asyncio.to_thread(thread.join, 2)
        if thread.is_alive():
            raise AssertionError("Delivery proxy listener did not stop.")
        if isinstance(proxy._threads, list):
            for worker in tuple(proxy._threads):
                await asyncio.to_thread(worker.join, 2)
                if worker.is_alive():
                    raise AssertionError("Delivery proxy CONNECT worker did not stop; scratch remains owned.")
        proxy.server_close()

    async def wait_published(self, count=1):
        async with asyncio.timeout(3):
            while sum(facts["event"] == "delivery_published" for _, facts in self.delivery_notices) < count:
                await asyncio.sleep(0.005)

    async def discover(self):
        discovered = await self.api.jobs(
            analysis_kind_ref=ANALYSIS_KIND_REF, has_provider_execution_attempt=False, limit=1, cursor=None
        )
        (selected,) = discovered.jobs
        return StartPending(
            self.config.lifecycle.scope,
            selected.analysis_kind_ref,
            selected.input,
            start_body(selected.input.job_ref, "runtime:one"),
        )

    async def _allocate_prediction(self):
        return await self.ports.allocate_prediction()

    def arguments(self, pending):
        return {
            "config": self.config,
            "pending": pending,
            "claims": self.claims,
            "stopping": self.stop,
            "admission_stopping": self.stop,
            "start_policy": LiveStartPolicy(
                cadence_seconds=self.config.lifecycle.poll_seconds,
                on_retry=lambda *facts: self.operator_notices.append(("start_retry", facts)),
                on_recovered=lambda *facts: self.operator_notices.append(("start_recovered", facts)),
                maximum_replays=self.config.lifecycle.start_replays,
                window_seconds=self.config.lifecycle.start_seconds,
            ),
            "execution_identity": {"image_id": "fixture"},
            "selected_artifacts": self.ports.worker.artifacts,
            "allocate_prediction": self.allocate_prediction,
            "on_prediction_failure": lambda failure, attempt: self.operator_notices.append(("prediction_failed", failure, attempt)),
            "on_interpreter_failure": lambda index, failure: self.operator_notices.append((index, failure)),
            "on_source_retry": lambda failure, seconds: self.operator_notices.append(("source_retry", failure, seconds)),
            "on_source_observation": lambda failure: self.operator_notices.append(("source_observation", failure)),
            "allocate_reader": self.ports.allocate_reader,
            "allocate_generator": self.ports.allocate_generator,
            "observation_recovery": ObservationRecovery(
                self.config.lifecycle.observation_gap_seconds,
                self.config.service.policy.api_backoff_max_seconds,
                self.observation_retry,
                self.observation_recovered,
            ),
            "on_unconfirmed_phase_progress": self.unconfirmed_phase_progress,
            "condition_recovery": None,
            "on_delivery_retry": lambda attempt, failure, seconds: self.delivery_notices.append(
                (failure, {"event": "producer_delivery_retry", "retry_seconds": seconds,
                           "execution_attempt_ref": attempt.admission.execution_attempt_ref})
            ),
        }

    def observation_retry(self, _attempt, _failure, _seconds):
        raise AssertionError("This unchanged scientific witness expects no observation outage")

    def unconfirmed_phase_progress(self, attempt, phase, failure):
        self.progress_notices.append((attempt, phase, failure))

    def observation_recovered(self, _attempt, _failure):
        raise AssertionError("This unchanged scientific witness expects no observation recovery")

    async def api_reply(self, request, body):
        method, target, _ = request.split(b"\r\n", 1)[0].decode().split(" ")
        path = urlsplit(target).path
        require_api_credentials(request)
        if path == "/provider/v1/jobs":
            document = page()
            document.update(analysis_kind_ref=ANALYSIS_KIND_REF, next_cursor=None)
            document["jobs"] = [
                {
                    **asdict(self.ports.instructions.identity),
                    "analysis_kind_ref": ANALYSIS_KIND_REF,
                    "created_at": "2026-10-18T00:00:00.123456Z",
                }
            ]
            return json.dumps(document).encode()
        if path.endswith("/input"):
            payload = b"PRIVATE changed after discovery" if self.corrupt_input else self.ports.instructions.specification_bytes
            return response(payload)
        if path.endswith("/uploads"):
            return encoded(
                membership(
                    uploads=[
                        member(
                            byte_length=len(self.ports.source),
                            content_hash="sha256:" + hashlib.sha256(self.ports.source).hexdigest(),
                        )
                    ]
                )
            )
        if path.endswith("/read-capability"):
            return encoded(
                grant(
                    byte_length=len(self.ports.source),
                    content_hash="sha256:" + hashlib.sha256(self.ports.source).hexdigest(),
                    download_url=self.store_peer.origin + "/upload/v1/uploads/" + UPLOAD_REF + "/bytes",
                    expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                )
            )
        if path.endswith("/start"):
            return start_response(analysis_kind_ref=ANALYSIS_KIND_REF)
        if path == "/provider/v1/execution-attempts/" + ATTEMPT_REF:
            return observation()
        if path.endswith("/progress"):
            return progress_response(phase=json.loads(body)["phase"])
        if path.endswith("/fail"):
            failure = json.loads(body)
            return fail_response(failure_code=failure["failure_code"], failure_message=failure["failure_message"])
        if path.endswith("/complete"):
            content = base64.b64decode(json.loads(body)["canonical_result_base64"], validate=True)
            self.result = json.loads(content)
            return complete_response(
                result_schema_id=self.result["schema_id"],
                result_byte_length=len(content),
                result_fingerprint="sha256:" + hashlib.sha256(content).hexdigest(),
            )
        raise AssertionError(f"Unexpected fixture route {method} {path}")

    async def model_reply(self, request, raw):
        require_model_credentials(request)
        last = json.loads(raw)["messages"][-1]
        if self.route == "smiles":
            return tool_reply("prepare", "prepare_smiles", {"smiles": "CCl", "requested_count": 2, "output": self.output})
        if self.route == "pasted":
            return tool_reply(
                "prepare",
                "prepare_pasted",
                {
                    "source_text": self.ports.source_text,
                    "source_format": "table",
                    "source_unit": "nm",
                    "output": self.output,
                },
            )
        if last["role"] != "tool":
            return tool_reply("inspect", "inspect_uploads", {})
        return tool_reply(
            "prepare",
            "prepare_upload",
            {
                "upload_ref": UPLOAD_REF,
                "source_format": "xyz",
                "source_unit": "angstrom",
                "selection": [2, 1],
                "alternate_location": None,
                "output": self.output,
            },
        )

    def verify_discovery_signature(self):
        request = self.api_peer.requests[0]
        target = request.split(b"\r\n", 1)[0].split(b" ")[1]
        headers = {
            name.lower(): value.strip()
            for line in request.split(b"\r\n")[1:]
            if b":" in line
            for name, value in [line.split(b":", 1)]
        }
        path, query = target.split(b"?", 1)
        base = b"\n".join(
            [
                b'"@method": GET',
                b'"@authority": ' + self.api_peer.origin.removeprefix("https://").encode(),
                b'"@path": ' + path,
                b'"@query": ?' + query,
                b'"@signature-params": ' + headers[b"signature-input"].split(b"=", 1)[1],
            ]
        )
        self.config.api.identity.private_key.public_key().verify(
            parse(headers[b"signature"], tltype="dictionary")["sig1"][0], base
        )
