#!/usr/bin/env python3
"""Minimal HTTP CONNECT proxy bound to one outbound interface."""

# Adapted from the SECS interface-bound proxy so acquisition uses the same
# SO_BINDTODEVICE transport; this repository runs the proxy inside a container.
# https://github.com/numpde/secs-repro/blob/34f5915c1534278bbf61b4c6592afa44cf94ab08/tools/bound_http_proxy.py

from __future__ import annotations

from tools.container_guard import require_container

require_container()

import argparse
import os
from pathlib import Path
import re
import select
import socket
import socketserver
import sys
import time
from urllib.parse import urlsplit


MAX_HEADER_BYTES = 64 * 1024
BUFFER_BYTES = 128 * 1024
HEADER_SECONDS = 30
RELAY_IDLE_SECONDS = 120


def _remaining(deadline: float, operation: str) -> float:
    """Return seconds left in one absolute budget, rejecting completed deadlines."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"Cannot complete proxy {operation}: its deadline elapsed.")
    return remaining


def receive_headers(client: socket.socket) -> tuple[bytes, bytes]:
    """Return the complete header including its terminator and any received body.

    One absolute 30-second budget covers every fragment. The byte limit covers
    only the header. Body bytes received with its final fragment remain
    available to the forwarding caller. EOF before the header
    completes raises ConnectionError; an oversized header raises ValueError.
    """
    deadline = time.monotonic() + HEADER_SECONDS
    data = bytearray()
    while True:
        # A fragment arriving after the deadline cannot complete admission.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L54-L56
        client.settimeout(_remaining(deadline, "header read"))
        chunk = client.recv(8192)
        _remaining(deadline, "header read")
        if not chunk:
            raise ConnectionError("client closed before sending headers")
        data.extend(chunk)
        terminator = data.find(b"\r\n\r\n")
        # Coalesced body bytes belong to forwarding, not header admission.
        # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py#L88
        header_bytes = terminator + 4 if terminator >= 0 else len(data)
        if header_bytes > MAX_HEADER_BYTES:
            raise ValueError("request headers are too large")
        if terminator >= 0:
            return bytes(data[:header_bytes]), bytes(data[header_bytes:])


def parse_destination(header: bytes) -> tuple[str, str, int, bytes]:
    """Admit a destination without transport and return its forwarding header.

    CONNECT retains its header; absolute HTTP targets become origin-form paths.
    Invalid destinations, including ports outside 1-65535, raise ValueError.
    """
    lines = header.split(b"\r\n")
    method, target, version = lines[0].decode("ascii").split(" ", 2)
    method = method.upper()

    if method == "CONNECT":
        host, separator, port_text = target.rpartition(":")
        if not separator or not host:
            raise ValueError("CONNECT target must be host:port")
    else:
        parsed = urlsplit(target)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("proxy request must use an absolute HTTP URL")
        host = parsed.hostname

    try:
        port = int(port_text) if method == "CONNECT" else parsed.port
    except ValueError:
        raise ValueError("Cannot admit proxy destination: the port must be an integer from 1 to 65535.") from None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    # Admit the destination before DNS or socket use; explicit HTTP port zero
    # must not silently choose the scheme default.
    # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py#L41
    if not 1 <= port <= 65535:
        raise ValueError("Cannot admit proxy destination: the port must be an integer from 1 to 65535.")
    if method == "CONNECT":
        return method, host, port, header

    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    lines[0] = f"{method} {path} {version}".encode("ascii")
    return method, host, port, b"\r\n".join(lines)


def connect_bound(host: str, port: int, interface: str) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, proto, _canonname, address in socket.getaddrinfo(
        host, port, family=socket.AF_INET, type=socket.SOCK_STREAM
    ):
        outbound = socket.socket(family, socktype, proto)
        try:
            outbound.setsockopt(
                socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0"
            )
            outbound.settimeout(30)
            outbound.connect(address)
            outbound.settimeout(None)
            return outbound
        except OSError as error:
            last_error = error
            outbound.close()
    raise last_error or OSError(f"could not resolve {host}")


def relay(left: socket.socket, right: socket.socket) -> None:
    sockets = (left, right)
    while True:
        readable, _, _ = select.select(sockets, (), (), 60)
        if not readable:
            continue
        for source in readable:
            data = source.recv(BUFFER_BYTES)
            if not data:
                return
            destination = right if source is left else left
            destination.sendall(data)


class ProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        """Forward one request, reporting failures without private request values.

        A fixed HTTP error is emitted only before forwarding is attempted. Once
        a write starts, its completion may be uncertain and a second HTTP status
        would corrupt the forwarded response or CONNECT tunnel.
        """
        phase = "request admission"
        try:
            header, remainder = receive_headers(self.request)
            method, host, port, forwarded_header = parse_destination(header)
            with connect_bound(host, port, self.server.outbound_interface) as outbound:
                # sendall may fail after writing a prefix; mark the phase before
                # the first HTTP header or CONNECT-success write is attempted.
                # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py#L296
                phase = "forwarding"
                # Header admission leaves its remaining timeout on the client;
                # forwarding owns a fresh bounded write budget for both peers.
                # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L312-L315
                self.request.settimeout(RELAY_IDLE_SECONDS)
                outbound.settimeout(RELAY_IDLE_SECONDS)
                if method == "CONNECT":
                    self.request.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                else:
                    outbound.sendall(forwarded_header)
                if remainder:
                    outbound.sendall(remainder)
                relay(self.request, outbound)
        except (ConnectionError, OSError, UnicodeError, ValueError) as error:
            if phase == "request admission":
                try:
                    self.request.settimeout(1)
                    self.request.sendall(
                        b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"
                    )
                except OSError:
                    pass
            # Native causes can contain request URLs, headers or other private
            # values. The operator receives only the owned phase and class.
            # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/repository_checks/test_image_build_proxy.py#L277
            print(f"proxy {phase} failed: {type(error).__name__}", file=sys.stderr)


class ProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], interface: str):
        self.outbound_interface = interface
        super().__init__(address, ProxyHandler)


def require_interface(interface: str) -> None:
    """Admit one native Linux interface name without using filesystem lookup."""
    # Native lookup admits the same device name consumed by SO_BINDTODEVICE.
    # https://github.com/numpde/magnet-deploy/blob/2d4ee50b0359e71900fce9289ea01f46d0f41249/scripts/image-build-proxy.py#L373-L381
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,14}", interface):
        raise ValueError("Cannot start proxy: require an ASCII interface name of 1 through 15 bytes, without paths or whitespace.")
    try:
        socket.if_nametoindex(interface)
    except OSError:
        raise ValueError("Cannot start proxy: the selected network interface does not exist.") from None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()

    try:
        require_interface(args.interface)
    except ValueError as error:
        raise SystemExit(str(error)) from None

    # Prove interface binding before publishing readiness, without any transfer.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(
            socket.SOL_SOCKET, socket.SO_BINDTODEVICE, args.interface.encode() + b"\0"
        )

    with ProxyServer(("127.0.0.1", args.port), args.interface) as server:
        port = server.server_address[1]
        args.ready_file.write_text(f"{port}\n", encoding="ascii")
        os.chmod(args.ready_file, 0o600)
        server.serve_forever()


if __name__ == "__main__":
    main()
