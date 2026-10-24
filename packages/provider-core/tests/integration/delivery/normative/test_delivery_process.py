"""The installed daemon command publishes through its explicit route and stops."""

import asyncio
import json
import signal
import sys
import tempfile
import unittest
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

from provider_core.delivery_client import DeliveryClient, DeliveryEndpoint
from provider_core.delivery_store import DeliveryQueue, QueueLimits
from tests.fakes.api_tls import api_tls_peer
from tests.fakes.attempts import fail_response
from tests.fakes.delivery import outcome
from tests.fakes.delivery_config import DeliveryConfigFiles
from tests.fakes.unix_proxy import UnixProxy


async def _reap_daemon(process):
    """Retire the original child even when an assertion or client cleanup fails.

    Ports tests/fakes/runner.py::close's TERM/KILL and original-handle wait;
    communicate drains the two pipes while waiting for this same process.
    """
    if process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(process.communicate(), 3)
    except TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await asyncio.wait_for(process.communicate(), 3)


class DeliveryProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_command_delivers_once_through_proxy_then_sigterm_retires_it(self):
        async with api_tls_peer("success", response_body=fail_response(
            execution_attempt_ref=outcome().execution_attempt_ref,
            failure_code="provider_interrupted", failure_message="SCIENTIFIC_PAYLOAD_NEEDLE"
        )) as peer:
            async with UnixProxy(peer.origin, "tunnel") as proxy:
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    files = DeliveryConfigFiles(root)
                    for name in ("state", "ipc"):
                        (root / name).mkdir(mode=0o700)
                    raw = files.raw.replace(b"https://api.example", peer.origin.encode())
                    raw = raw.replace(json.dumps(str(root / "api/http.sock")).encode(), json.dumps(proxy.path).encode())
                    raw += b"ca_file = " + json.dumps(str(Path(peer.scratch.name) / "cert.pem")).encode() + b"\n"
                    files.write(raw)
                    process = await asyncio.create_subprocess_exec(
                        sys.executable, "-I", "-B", "-m", "provider_core.delivery_main", "--config", str(files.path),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    )
                    endpoint = DeliveryEndpoint(peer.origin, "provider:example", root / "ipc/http.sock")
                    client = DeliveryClient(endpoint, exchange_seconds=1, retirement_seconds=0.2)
                    notices = []

                    async def receive(event):
                        async with asyncio.timeout(3):
                            while line := await process.stderr.readline():
                                report = json.loads(line)
                                notices.append(report)
                                if report["event"] == event:
                                    return report
                        self.fail(f"Daemon exited before {event}.")

                    try:
                        await receive("delivery_ready")
                        item = replace(outcome(provider="provider:example"), api_origin=peer.origin)
                        self.assertEqual(await client.submit(item), item.acceptance())
                        await receive("delivery_published")
                        self.assertEqual((await client.pending())["items"], [])
                        self.assertEqual(peer.request_bodies, [item.body])
                        self.assertEqual(len(proxy.headers), 1)
                        process.send_signal(signal.SIGTERM)
                        stopped = await receive("delivery_stopped")
                        self.assertTrue(stopped["retirement_confirmed"])
                        self.assertEqual(await asyncio.wait_for(process.wait(), 3), 0)
                        self.assertNotIn("SCIENTIFIC_PAYLOAD_NEEDLE", json.dumps(notices))
                        self.assertFalse(endpoint.socket_path.exists())
                        reopened = DeliveryQueue(root / "state", limits=QueueLimits(4, 16384, 8, 32768))
                        try:
                            self.assertEqual(reopened.inventory(item.api_origin, item.provider_ref), [])
                        finally:
                            reopened.close()
                    finally:
                        try:
                            await client.retire()
                        finally:
                            await _reap_daemon(process)

    async def test_bad_configuration_has_safe_receiving_diagnostics_and_no_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            files = DeliveryConfigFiles(directory)
            files.write(b"PRIVATE_CANARY malformed TOML")
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-B", "-m", "provider_core.delivery_main", "--config", str(files.path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 3)
                self.assertEqual(process.returncode, 2)
                self.assertEqual(stdout, b"")
                report = json.loads(stderr)
                self.assertEqual(report["event"], "delivery_failed")
                self.assertIn("UTF-8 TOML", json.dumps(report))
                self.assertNotIn("PRIVATE", stderr.decode())
                self.assertFalse((Path(directory) / "state").exists())
            finally:
                await _reap_daemon(process)
