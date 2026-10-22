"""Disposable retained obligations and signed TLS; no scientific worker exists."""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempt_store import admission, record
from tests.fakes.attempts import ATTEMPT_REF, complete_response, fail_response, observation, start_response
from tests.fakes.publication import retained_failure
from tests.integration.nmrnet.config_ports import ConfigFiles
from tests.integration.nmrnet.process_ports import JoinedServiceProxy
from tests.integration.nmrnet.test_live_worker import verify_signed_request

from nmrnet_provider.config import load_recovery_config
from nmrnet_provider.recovery import main
from provider_core.attempt_files import AttemptFiles
from provider_core.attempt_record import (
    ProtectedEvidence,
    SuppressedOutcome,
    SuppressionReason,
    TerminalHeld,
    TerminalOperation,
    TerminalPending,
    acknowledge_start,
    encode_record,
    mark_execution_entered,
)
from provider_core.attempt_request import complete_body, fail_body
from provider_core.attempt_store import AttemptStore
from tools.provider_proxy import ServicePolicy


class RecoveryPeers:
    async def __aenter__(self):
        self.stack = AsyncExitStack()
        try:
            self.peer = await self.stack.enter_async_context(api_tls_peer("success", response_factory=self.reply))
            root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
            self.files = ConfigFiles(
                root, api_origin=self.peer.origin, ca_file=Path(self.peer.scratch.name) / "cert.pem", maximum_records=3
            )
            self.files.key.unlink()
            self.config = load_recovery_config(self.files.path)
            self.diagnostics = root / "diagnostics"
            self.diagnostics.mkdir(mode=0o700)
            self.files.state.mkdir(mode=0o700)
            self.response = fail_response(replayed=True)
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def __aexit__(self, *error):
        return await self.stack.__aexit__(*error)

    async def reply(self, request, body):
        verify_signed_request(request, body, origin=self.peer.origin, key=self.config.api.identity.private_key)
        return self.response

    def store(self):
        settings = self.config.lifecycle
        return AttemptStore(
            settings.store_root, scope=settings.scope, limits=settings.store_limits, operation_seconds=settings.store_seconds
        )

    async def retain(self, phase="terminal"):
        if phase in ("terminal", "held"):
            store, current = await retained_failure(self.files.state, self.config.lifecycle.scope)
            if phase == "held":
                held = TerminalHeld(current, "execution_attempt_failure_replay_mismatch", "request:held")
                await store.replace(current, held)
                current = held
        else:
            store = self.store()
            await store.open()
            current = replace(record(), scope=self.config.lifecycle.scope)
            await store.admit(current)
            if phase == "suppressed":
                active = acknowledge_start(current, admission())
                await store.replace(current, active)
                candidate = TerminalPending(
                    active,
                    TerminalOperation.FAIL,
                    fail_body(ATTEMPT_REF, "input_rejected", "private result message"),
                    ProtectedEvidence("fixture.result.v1", b"protected original cause"),
                )
                current = SuppressedOutcome(candidate, SuppressionReason.CALLER_CANCELLED)
                await store.replace(active, current)
            if phase in ("entered", "complete"):
                active = acknowledge_start(current, admission())
                await store.replace(current, active)
                entered = mark_execution_entered(active, ProtectedEvidence("fixture.prepared.v1", b"private candidate"))
                await store.replace(active, entered)
                current = entered
                self.response = observation()
                if phase == "complete":
                    public = b'{"output":{"shift_significant_digits":4},"shift_ppm":32.31}'
                    evidence = ProtectedEvidence("fixture.unrounded.v1", b'{"shift_ppm":32.309967041015625}')
                    current = TerminalPending(
                        entered,
                        TerminalOperation.COMPLETE,
                        complete_body(ATTEMPT_REF, "fixture.result.v1", public),
                        evidence,
                    )
                    await store.replace(entered, current)
                    self.response = complete_response(
                        replayed=True,
                        result_schema_id="fixture.result.v1",
                        result_fingerprint="sha256:" + hashlib.sha256(public).hexdigest(),
                        result_byte_length=len(public),
                    )
            else:
                self.response = start_response(replayed=True)
        await store.close()
        self.record = current
        return hashlib.sha256(encode_record(current)).hexdigest()

    async def records(self):
        store = self.store()
        await store.open()
        try:
            return await store.records()
        finally:
            await store.close()

    def result(self):
        return json.loads((self.diagnostics / "result.json").read_bytes())


async def observed_file(path):
    async with asyncio.timeout(3):
        while not path.exists():
            await asyncio.sleep(0.01)


def child_environment():
    return {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": "/consumer" + os.pathsep + str(Path.cwd()),
        "NMRNET_IN_CONTAINER": "1",
        "PROVIDER_CORE_IN_CONTAINER": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }


async def held_inspection_child(files, diagnostics):
    """The parent owns exact SIGKILL; the original storage thread never releases."""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-B",
        str(Path(__file__)),
        "--hold-storage",
        "inspect",
        "--config",
        str(files.path),
        "--diagnostics",
        str(diagnostics),
        env=child_environment(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


@asynccontextmanager
async def recovery_child(fixture, operation, digest=None):
    """Run the real CLI with the existing API-only Unix proxy component."""
    proxy = thread = process = None
    arguments = [
        "-m",
        "nmrnet_provider.recovery",
        operation,
        "--config",
        str(fixture.files.path),
        "--diagnostics",
        str(fixture.diagnostics),
    ]
    try:
        if operation == "run":
            port = urlsplit(fixture.peer.origin).port
            policy = ServicePolicy.admit("local", [f"127.0.0.1:{port}"], ["127.0.0.1=127.0.0.1"])
            proxy = JoinedServiceProxy(fixture.files.root / "recovery.sock", "lo", policy)
            thread = threading.Thread(target=proxy.serve_forever, kwargs={"poll_interval": 0.01})
            thread.start()
            arguments.extend(["--record-sha256", digest, "--api-proxy-socket", proxy.server_address])
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-B",
            *arguments,
            env=child_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        yield process
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.communicate()
        if thread is not None:
            await asyncio.to_thread(proxy.shutdown)
            await asyncio.to_thread(thread.join, 2)
            if thread.is_alive():
                raise AssertionError("Recovery proxy listener did not stop")
        if proxy is not None:
            if isinstance(proxy._threads, list):
                for worker in tuple(proxy._threads):
                    await asyncio.to_thread(worker.join, 2)
                    if worker.is_alive():
                        raise AssertionError("Recovery proxy worker did not stop")
            proxy.server_close()


def held_inspection_main():
    sys.argv.remove("--hold-storage")
    diagnostics = Path(sys.argv[sys.argv.index("--diagnostics") + 1])
    original = AttemptFiles.records
    calls = 0

    def held_read(owner):
        nonlocal calls
        calls += 1
        if calls == 1:
            return original(owner)
        (diagnostics / "read-entered").write_bytes(b"observed\n")
        threading.Event().wait()
        raise AssertionError("The parent must kill this exact child before the original read can finish")

    try:
        with patch.object(AttemptFiles, "records", held_read):
            raise SystemExit(main())
    finally:
        (diagnostics / "recovery-main-exited").write_bytes(b"observed\n")


if __name__ == "__main__":
    held_inspection_main()
