"""Serve a private CONNECT route for explicitly granted service authorities.

The host supplies admitted configuration authorities and, for a named internal
network, its observed member addresses. This process does not discover peers or
claim those observations remain current. TLS authenticates the original name in
the controller. The host must retire this exact container after controller drain;
closing a daemon-thread listener does not establish tunnel retirement.

Header, thread, device-bound DNS/TCP and relay owners are reused from
tools.bound_http_proxy (its pinned Magnet/SECS sources are documented there).
"""

from tools.container_guard import require_container

require_container()

import argparse
import ipaddress
import logging
import os
import re
import socket
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from tools import bound_http_proxy


class ServiceRefused(ValueError):
    """Owned safe refusal; request/configuration values never enter its text."""


def native_failure_detail(error):
    """Project owned DNS reasons or native type/errno, never arbitrary text/notes."""
    if type(error) in (bound_http_proxy.DNSResponseRejected, bound_http_proxy.DNSNameRejected):
        return f"{type(error).__name__}: {error}"
    if isinstance(error, OSError):
        return f"{type(error).__name__}; errno={error.errno}"
    return type(error).__name__


def authority_parts(authority):
    """Admit the exact ASCII host:port representation emitted by configuration."""
    host, separator, port_text = authority.rpartition(":")
    if not separator or not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", host):
        raise ServiceRefused(
            "Cannot admit service proxy authority: require a canonical DNS name or IPv4 address and explicit port."
        )
    try:
        bound_http_proxy._dns_name(host)
        port = int(port_text)
    except (UnicodeError, ValueError):
        raise ServiceRefused("Cannot admit service proxy authority: its DNS name or port is malformed.") from None
    if not 1 <= port <= 65535 or port_text != str(port):
        raise ServiceRefused("Cannot admit service proxy authority: require a canonical port from 1 through 65535.")
    return host, port


@dataclass(frozen=True, slots=True)
class ServicePolicy:
    """Exact destination grant and optional frozen local address projection."""

    authorities: frozenset[tuple[str, int]]
    addresses: tuple[tuple[str, str], ...]

    @classmethod
    def admit(cls, mode, authorities, addresses):
        if mode not in ("local", "mapped", "external"):
            raise ServiceRefused("Cannot configure service proxy: select local, mapped or external routing explicitly.")
        admitted = frozenset(authority_parts(value) for value in authorities)
        if not admitted or len(admitted) != len(authorities):
            raise ServiceRefused("Cannot configure service proxy: authorities must be nonempty and unique.")
        mapped = {}
        for value in addresses:
            host, separator, text = value.partition("=")
            try:
                address = str(ipaddress.IPv4Address(text))
            except ipaddress.AddressValueError:
                raise ServiceRefused("Cannot configure local service proxy: each peer requires a literal IPv4 address.") from None
            if not separator or host in mapped:
                raise ServiceRefused("Cannot configure local service proxy: peer address assignments must be unambiguous.")
            try:
                literal = ipaddress.IPv4Address(host)
            except ipaddress.AddressValueError:
                literal = None
            if literal is not None and str(literal) != address:
                raise ServiceRefused("Cannot configure local service proxy: a literal authority must keep its own address.")
            mapped[host] = address
        if mode == "external" and mapped:
            raise ServiceRefused("Cannot configure external service proxy: local address assignments are not allowed.")
        if mode in ("local", "mapped") and mapped.keys() != {host for host, _ in admitted}:
            raise ServiceRefused("Cannot configure mapped service proxy: require exactly one address for every admitted host.")
        return cls(admitted, tuple(sorted(mapped.items())))

    def destination(self, header):
        if not header.startswith(b"CONNECT "):
            raise ServiceRefused("Cannot admit service proxy request: only CONNECT tunnels are allowed.")
        try:
            destination = bound_http_proxy.parse_destination(header)
            _, host, port, _ = destination
            target = header.split(b" ", 2)[1].decode("ascii")
            if authority_parts(target) != (host, port):
                raise ValueError
        except (UnicodeError, ValueError):
            raise ServiceRefused("Cannot admit service proxy request: the CONNECT authority is malformed.") from None
        if (host, port) not in self.authorities:
            raise ServiceRefused(
                "Cannot admit service proxy request: this destination is outside the configured authority grant."
            )
        return destination

    def connect(self, host, port, interface):
        """Explicit addresses use the existing literal shortcut, with no DNS fallback."""
        address = dict(self.addresses)[host] if self.addresses else host
        return bound_http_proxy.connect_bound(address, port, interface)


class ServiceProxyHandler(bound_http_proxy.ProxyHandler):
    def destination(self, header):
        return self.server.policy.destination(header)

    def connect(self, host, port):
        return self.server.policy.connect(host, port, self.server.outbound_interface)

    def admission_reply(self, error):
        if isinstance(error, ServiceRefused):
            return b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n"
        return super().admission_reply(error)

    def report_failure(self, phase, error):
        if isinstance(error, ServiceRefused):
            sys.stderr.write(str(error) + "\n")
        elif isinstance(error, bound_http_proxy.ProxyResolutionFailed):
            sys.stderr.write(str(error) + "\n")
            # Eight one-line native causes bound operator output; the DNS
            # owner retains all underlying evidence on the exception.
            for failure in error.failures[:8]:
                sys.stderr.write(f"Service proxy DNS cause: {native_failure_detail(failure)}.\n")
            if len(error.failures) > 8:
                sys.stderr.write(
                    f"Service proxy DNS: {len(error.failures) - 8} additional causes omitted from operator output.\n"
                )
        else:
            sys.stderr.write(f"Service proxy {phase} failed: {native_failure_detail(error)}. Request contents are not logged.\n")


class ServiceProxyServer(bound_http_proxy.ProxyServer):
    """Reuse bounded workers/relay on an exclusively created Unix listener."""

    address_family = socket.AF_UNIX
    request_handler = ServiceProxyHandler

    def __init__(self, path, interface, policy):
        self.policy = policy
        super().__init__(str(path), interface)

    def server_bind(self):
        # TCPServer.__init__ closes the acquired socket when this phase fails.
        # Admit permissions before activation, keeping that existing owner.
        super().server_bind()
        Path(self.server_address).chmod(0o600)


def require_socket_grant(path, ready):
    """Refuse old evidence before acquiring any listener; never unlink a leaf."""
    if (
        not path.is_absolute()
        or not ready.is_absolute()
        or path == ready
        or path.parent != ready.parent
        or "\0" in str(path) + str(ready)
    ):
        raise ServiceRefused(
            "Cannot start service proxy: socket and readiness require distinct absolute paths in one private directory."
        )
    try:
        if len(str(path).encode("utf-8")) > 107:
            raise ServiceRefused("Cannot start service proxy: the Unix socket path exceeds Linux's 107-byte pathname limit.")
        parent = path.parent.lstat()
        if (
            path.parent.resolve() != path.parent
            or not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise ServiceRefused(
                "Cannot start service proxy: its directory must be caller-owned, private (0700), and free of symlinks."
            )
        for leaf in (path, ready):
            if leaf.exists() or leaf.is_symlink():
                raise ServiceRefused(
                    "Cannot start service proxy: socket/readiness evidence already exists; inspect the retained run first."
                )
    except OSError as error:
        raise ServiceRefused(
            f"Cannot start service proxy: its private filesystem grant could not be admitted ({type(error).__name__}; errno={error.errno})."
        ) from error
    except UnicodeError as error:
        raise ServiceRefused("Cannot start service proxy: its Unix path must be valid UTF-8.") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", required=True)
    parser.add_argument("--mode", choices=("local", "mapped", "external"), required=True)
    parser.add_argument("--authority", action="append", required=True)
    parser.add_argument("--address", action="append")
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        # Omitted --address supplies no mappings. External mode requires none;
        # local mode still requires complete coverage in ServicePolicy.admit.
        addresses = args.address
        if addresses is None:
            addresses = ()
        policy = ServicePolicy.admit(args.mode, args.authority, addresses)
        require_socket_grant(args.socket, args.ready_file)
        try:
            bound_http_proxy.require_interface(args.interface)
        except ValueError as error:
            raise ServiceRefused(str(error)) from None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, args.interface.encode() + b"\0")
        with ServiceProxyServer(args.socket, args.interface, policy) as server:
            bound_http_proxy.publish_readiness(args.ready_file, "ready\n")
            logging.getLogger("nmrnet_provider.proxy").info("Service proxy ready.")
            server.serve_forever()
    except ServiceRefused as error:
        raise SystemExit(str(error)) from None
    except OSError as error:
        raise SystemExit(
            f"Cannot run service proxy: {type(error).__name__}; errno={error.errno}. Inspect the retained run before restarting."
        ) from None


if __name__ == "__main__":
    from nmrnet_provider.runtime_logging import configure_logging

    configure_logging(os.environ.get("NMRNET_LOG_LEVEL", "INFO"))
    main()
