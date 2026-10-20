"""Actual contained foreground subprocess against disposable signed local TLS.

Metadata and model declarations are authored test inputs, not deployed/image or
scientific evidence. This fixture supplies no Jobs unless a test asks for one.
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from tests.fakes.api import response_headers
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempt_inventory import response as inventory_response
from tests.fakes.hello import receipt
from tests.fakes.job_feed import job, page
from tests.fakes.job_input import facts
from tests.integration.nmrnet.config_ports import ConfigFiles
from tests.integration.nmrnet.test_live_worker import verify_signed_request

from nmrnet_provider.config import load_provider_config
from provider_core.attempt_record import StartPending
from provider_core.attempt_request import start_body
from provider_core.attempt_store import AttemptStore


class ProcessPeers:
    def __init__(self):
        self.process = None
        self.events = []
        self.discovered = asyncio.Event()
        self.bad_reply = False
        self.offer_job = False

    async def __aenter__(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.root = Path(self.scratch.name)
        self.peer = await api_tls_peer(
            "success",
            response_factory=self.reply,
            response_headers=(*response_headers(request_id=b"request:process-proof"), (b"x-private", b"PRIVATE_HEADER")),
        ).__aenter__()
        # Leave a spare slot: the restart refusal must be caused by the retained
        # obligation, rather than the separate live-capacity admission gate.
        self.files = ConfigFiles(
            self.root, api_origin=self.peer.origin, ca_file=Path(self.peer.scratch.name) / "cert.pem", maximum_records=2
        )
        self.config = load_provider_config(self.files.path)
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
        return self

    async def __aexit__(self, *failure):
        try:
            if self.process is not None and self.process.returncode is None:
                self.process.kill()
                await self.process.communicate()
        finally:
            try:
                await self.peer.__aexit__(*failure)
            finally:
                self.scratch.cleanup()

    # silent-defaults: allow-contract -- Ordinary subprocess tests run the real module; only the held-owner test selects the child-only fault shim.
    async def start(self, *, held_work=False):
        environment = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": "/consumer",
            "NMRNET_IN_CONTAINER": "1",
            "PROVIDER_CORE_IN_CONTAINER": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        # silent-defaults: allow-contract -- Select the explicitly requested fault shim or actual process entrypoint without changing production configuration.
        command = [str(Path(__file__).with_name("held_process.py"))] if held_work else ["-m", "nmrnet_provider.process"]
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
        store = AttemptStore(
            self.files.state,
            scope=self.config.lifecycle.scope,
            limits=self.config.lifecycle.store_limits,
            operation_seconds=self.config.lifecycle.store_seconds,
        )
        try:
            await store.open()
            return await store.records()
        finally:
            await store.close()

    async def seed_pending(self):
        self.files.state.mkdir(mode=0o700)
        store = AttemptStore(
            self.files.state,
            scope=self.config.lifecycle.scope,
            limits=self.config.lifecycle.store_limits,
            operation_seconds=self.config.lifecycle.store_seconds,
        )
        source = facts()
        pending = StartPending(self.config.lifecycle.scope, "peaks_from_mol", source, start_body(source.job_ref, "a" * 32))
        try:
            await store.open()
            await store.admit(pending)
        finally:
            await store.close()
        return pending
