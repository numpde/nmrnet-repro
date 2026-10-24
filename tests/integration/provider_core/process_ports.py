"""Actual contained foreground subprocess against disposable signed local TLS.

Metadata and model declarations are authored test inputs, not deployed/image or
scientific evidence. This fixture supplies no Jobs unless a test asks for one.
"""

import asyncio
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit

from tests.fakes.api import response_headers
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempt_inventory import response as inventory_response
from tests.fakes.hello import receipt
from tests.fakes.job_feed import job, page
from tests.fakes.job_input import facts
from tests.integration.nmrnet.config_ports import ConfigFiles
from tests.integration.nmrnet.runtime_ports import RuntimePeers
from tests.integration.nmrnet.smiles_ports import InputPorts
from tests.fakes.api_tls import verify_signed_request

from nmrnet_provider.config import load_provider_config
from provider_core.attempt_record import StartPending
from provider_core.attempt_request import start_body
from provider_core.api import ProviderAPI
from provider_core.checkpoint_store import CheckpointStore
from provider_core.delivery_service import DeliveryBinding, DeliveryService
from provider_core.delivery_store import QueueLimits
from tools.provider_proxy import ServicePolicy, ServiceProxyServer


class JoinedServiceProxy(ServiceProxyServer):
    # CPython v3.12.12 Lib/socketserver.py::ThreadingMixIn retains every
    # non-daemon worker before starting it. The fixture joins that same owner,
    # including a thread whose handler has not yet entered, after peer closure.
    daemon_threads = False


async def read_process_failure(process):
    """Receive the actual stderr report while a failed owner can remain held."""
    async with asyncio.timeout(5):
        while True:
            line = await process.stderr.readline()
            if not line:
                raise AssertionError("Foreground process closed stderr before its failure report")
            if json.loads(line).get("exception_type") == "ProviderProcessFailed":
                return line


class ProcessPeers:
    def __init__(self, *, model_peer=None):
        self.process = None
        self.events = []
        self.discovered = asyncio.Event()
        self.bad_reply = False
        self.offer_job = False
        self.peer = self.proxy = self.proxy_thread = None
        self.relay = self.relay_task = None
        self.delivery_notices = []
        self.model_peer = model_peer
        self.ports = InputPorts()
        self.result = None
        self.corrupt_input = False

    async def __aenter__(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)
        try:
            return await self._open()
        except BaseException:
            await self.__aexit__(*sys.exc_info())
            raise

    async def _open(self):
        self.peer = await api_tls_peer(
            "success",
            response_factory=self.reply,
            response_headers=(*response_headers(request_id=b"request:process-proof"), (b"x-private", b"PRIVATE_HEADER")),
        ).__aenter__()
        # Leave a spare slot: the restart refusal must be caused by the retained
        # obligation, rather than the separate live-capacity admission gate.
        model_options = {}
        if self.model_peer is not None:
            model_options = {
                "model_origin": self.model_peer.origin,
                "model_ca_file": Path(self.model_peer.scratch.name) / "cert.pem",
            }
        self.files = ConfigFiles(
            self.root,
            api_origin=self.peer.origin,
            ca_file=Path(self.peer.scratch.name) / "cert.pem",
            maximum_records=2,
            **model_options,
        )
        self.config = load_provider_config(self.files.path)
        relay_root = self.root / "relay-state"
        relay_root.mkdir(mode=0o700)
        self.config.delivery.endpoint.socket_path.parent.mkdir(mode=0o700)
        publisher = ProviderAPI(self.config.api.identity, self.config.api.http.new_transport(),
                                expected_topology=self.config.api.expected_topology)
        self.relay = DeliveryService(
            relay_root, limits=QueueLimits(8, 16777216, 8, 16777216),
            bindings=(DeliveryBinding(self.config.delivery.endpoint, publisher),), request_seconds=2,
            retry_initial_seconds=0.01, retry_max_seconds=0.1,
            on_notice=lambda error, facts: self.delivery_notices.append((error, facts)),
            diagnostic_status=lambda: {"output_failures": 0},
        )
        self.relay_task = asyncio.create_task(self.relay.run())
        await asyncio.wait_for(self.relay.ready.wait(), 2)
        self.diagnostics = self.root / "diagnostics"
        self.diagnostics.mkdir(mode=0o700)
        self.execution = self.root / "execution.json"
        self.models = self.root / "models.json"
        self.execution.write_text(
            json.dumps(
                {
                    "image_id": "sha256:" + "a" * 64,
                    "geometry_image_id": "sha256:" + "b" * 64,
                    "controller_image_id": "sha256:" + "c" * 64,
                    "repository_revision": "b" * 40,
                    "upstream_revision": "c" * 40,
                    "runtime_identity_source": "caller",
                    "tool_sources": {"nmrnet_provider/process.py": {"size": 1, "sha256": "a" * 64}},
                }
            )
        )
        member = {"member": "fixture/member", "size": 1, "sha256": "c" * 64}
        self.models.write_text(
            json.dumps(
                {
                    "dictionary": {"archive_sha256": "d" * 64, **member},
                    "checkpoint_archive": {"archive_sha256": "e" * 64},
                    "elements": {element: {"checkpoint": member, "scaler": member} for element in ("H", "C")},
                }
            )
        )
        for path in (self.execution, self.models):
            path.chmod(0o600)
        port = urlsplit(self.peer.origin).port
        authorities = [f"127.0.0.1:{port}"]
        if self.model_peer is not None:
            authorities.append(f"127.0.0.1:{urlsplit(self.model_peer.origin).port}")
        policy = ServicePolicy.admit("local", authorities, ["127.0.0.1=127.0.0.1"])
        self.proxy = JoinedServiceProxy(self.root / "proxy.sock", "lo", policy, status_directory=self.root)
        self.proxy_thread = threading.Thread(target=self.proxy.serve_forever, kwargs={"poll_interval": 0.01})
        self.proxy_thread.start()
        self.proxy_arguments = [
            part
            for role in ("api", "interpreter", "store")
            for part in ("--" + role + "-proxy-socket", self.proxy.server_address)
        ]
        return self

    async def __aexit__(self, *failure):
        try:
            if self.process is not None and self.process.returncode is None:
                self.process.kill()
                await self.process.communicate()
        finally:
            if self.relay_task is not None:
                # Reuse the existing real relay's stop/join/close owner before
                # releasing its API peer or any producer socket grants.
                await RuntimePeers.close_relay(self)
            try:
                if self.proxy_thread is not None and self.proxy_thread.is_alive():
                    await asyncio.to_thread(self.proxy.shutdown)
                    await asyncio.to_thread(self.proxy_thread.join, 2)
                    if self.proxy_thread.is_alive():
                        raise AssertionError("Process fixture proxy listener did not stop.")
            finally:
                try:
                    if self.peer is not None:
                        await self.peer.__aexit__(*failure)
                finally:
                    if self.proxy is not None:
                        # Before the first request, socketserver uses a
                        # no-thread sentinel; its list owns all started workers.
                        if isinstance(self.proxy._threads, list):
                            for worker in tuple(self.proxy._threads):
                                await asyncio.to_thread(worker.join, 2)
                                if worker.is_alive():
                                    raise AssertionError("Process fixture CONNECT worker did not stop; scratch remains owned.")
                        self.proxy.server_close()
                    self.scratch.cleanup()

    def stall_periodic_hello(self):
        """Complete startup Hello, then return only headers for the next one."""

        def response_mode(request, _body):
            hello = b"/hello HTTP/1.1"
            if (
                request.split(b"\r\n", 1)[0].endswith(hello)
                and sum(item.split(b"\r\n", 1)[0].endswith(hello) for item in self.peer.requests) > 1
            ):
                return "body-stall"
            return "success"

        self.peer.response_mode = response_mode

    async def start(self, *, child_script=None):
        environment = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": "/consumer",
            "NMRNET_IN_CONTAINER": "1",
            "PROVIDER_CORE_IN_CONTAINER": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        # The contained receiving fixture records startup native barriers only;
        # real runner barrier proof belongs to the separate deployment lane.
        # Each script still invokes the actual foreground process entrypoint.
        environment["PYTHONPATH"] += os.pathsep + str(Path.cwd())
        script = child_script if child_script is not None else "startup_process.py"
        command = [str(Path(__file__).with_name(script))]
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            *command,
            "--config",
            str(self.files.path),
            "--execution",
            str(self.execution),
            "--models",
            str(self.models),
            "--diagnostics",
            str(self.diagnostics),
            *self.proxy_arguments,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return self.process

    async def reply(self, request, body):
        verify_signed_request(request, body, origin=self.peer.origin, key=self.config.api.identity.private_key)
        path = urlsplit(request.split(b"\r\n", 1)[0].decode().split()[1]).path
        self.events.append(path.rsplit("/", 1)[-1])
        if self.bad_reply:
            return b"PRIVATE_BODY Bearer PRIVATE_MODEL_KEY"
        if path.endswith("/execution-attempts"):
            return inventory_response(attempts=[], next_cursor=None)
        if path.endswith("/hello"):
            return receipt()
        if self.model_peer is not None:
            # Reuse the signed Job/Attempt wire fixtures; the subprocess owns
            # its own store and the external model peer is owned by the test.
            self.api_peer = self.peer
            return await RuntimePeers.api_reply(self, request, body)
        if not path.endswith("/jobs"):
            raise AssertionError("Foreground fixture permits only inventory, Hello and discovery")
        self.discovered.set()
        result = page()
        # silent-defaults: allow-contract -- The fixture offers one eligible Job only when the test requests work; idle startup and refusal cases discover none.
        jobs = [{**job(), "analysis_kind_ref": "peaks_from_mol"}] if self.offer_job else []
        result.update(analysis_kind_ref="peaks_from_mol", jobs=jobs, next_cursor=None)
        return json.dumps(result).encode()

    async def observed(self, name):
        async def wait():
            while not (self.diagnostics / name).exists():
                if self.process is not None and self.process.returncode is not None:
                    _, stderr = await self.process.communicate()
                    raise AssertionError("Foreground process exited before " + name + ": " + stderr.decode())
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait(), 5)

    async def reopen_store(self):
        store = CheckpointStore(
            self.files.state,
            scope=self.config.lifecycle.scope,
            limits=self.config.lifecycle.store_limits,
            operation_seconds=self.config.lifecycle.store_seconds,
        )
        try:
            await store.open()
            return await store.inventory()
        finally:
            await store.close()

    async def seed_pending(self):
        self.files.state.mkdir(mode=0o700)
        store = CheckpointStore(
            self.files.state,
            scope=self.config.lifecycle.scope,
            limits=self.config.lifecycle.store_limits,
            operation_seconds=self.config.lifecycle.store_seconds,
        )
        source = facts()
        pending = StartPending(self.config.lifecycle.scope, "peaks_from_mol", source, start_body(source.job_ref, "a" * 32))
        try:
            await store.open()
            await store.retain_start(pending)
        finally:
            await store.close()
        return pending
