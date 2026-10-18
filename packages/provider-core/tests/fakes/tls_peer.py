"""Finite loopback TLS peer; all keys and certificates live in disposable scratch."""

import asyncio
import ipaddress
import ssl
import tempfile
from builtins import BaseExceptionGroup
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID


class TlsPeer:
    def __init__(self, mode):
        self.mode = mode
        self.received = asyncio.Event()
        self.closed = asyncio.Event()
        self.handlers = set()
        self.requests = []

    async def __aenter__(self):
        self.scratch = tempfile.TemporaryDirectory()
        root = Path(self.scratch.name)
        key = Ed25519PrivateKey.generate()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "provider-core test peer")])
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
            .sign(key, None)
        )
        (root / "key.pem").write_bytes(
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        )
        (root / "cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        server_trust = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_trust.load_cert_chain(root / "cert.pem", root / "key.pem")
        self.trust = ssl.create_default_context(cafile=str(root / "cert.pem"))
        self.server = await asyncio.start_server(
            self.handle, "127.0.0.1", 0, ssl=None if self.mode == "tls-stall" else server_trust
        )
        self.origin = f"https://127.0.0.1:{self.server.sockets[0].getsockname()[1]}"
        return self

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        try:
            if self.mode == "tls-stall":
                await reader.readexactly(1)
                self.received.set()
                await reader.read()
                self.closed.set()
                return
            self.requests.append(await reader.readuntil(b"\r\n\r\n"))
            self.received.set()
            if self.mode == "success":
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ndata")
            elif self.mode == "redirect":
                writer.write(b"HTTP/1.1 302 Found\r\nLocation: https://elsewhere.invalid\r\nContent-Length: 0\r\n\r\n")
            elif self.mode in {"body-stall", "trickle", "disconnect"}:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nx")
            elif self.mode != "headers-stall":
                raise ValueError("Unknown test peer mode")
            await writer.drain()
            if self.mode == "disconnect":
                return
            if self.mode == "trickle":
                for _ in range(30):
                    await asyncio.sleep(0.02)
                    writer.write(b"x")
                    await writer.drain()
            await reader.read()
        except (ConnectionError, asyncio.IncompleteReadError):
            # A deliberately interrupted exchange may close either TLS direction.
            self.closed.set()
        else:
            self.closed.set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                self.closed.set()

    async def __aexit__(self, *exception):
        self.server.close()
        handlers = tuple(self.handlers)
        cancelled = {task for task in handlers if not task.done()}
        for task in cancelled:
            task.cancel()
        try:
            results = await asyncio.wait_for(asyncio.gather(*handlers, return_exceptions=True), 2)
            await asyncio.wait_for(self.server.wait_closed(), 2)
        finally:
            self.scratch.cleanup()
        failures = [
            result
            for task, result in zip(handlers, results, strict=True)
            if isinstance(result, BaseException) and not (task in cancelled and isinstance(result, asyncio.CancelledError))
        ]
        if failures:
            raise BaseExceptionGroup("TLS test peer failed", failures)
