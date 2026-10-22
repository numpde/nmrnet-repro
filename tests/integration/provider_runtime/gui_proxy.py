"""GUI receiver qualification's TCP listener for the existing service policy."""

from tools.container_guard import require_container

require_container()

import argparse
import socket
from pathlib import Path
from urllib.parse import urlsplit

from tools.bound_http_proxy import ProxyServer, publish_readiness, require_interface
from tools.provider_proxy import ServicePolicy, ServiceProxyHandler


class BrowserProxy(ProxyServer):
    """Keep service destination/relay policy; Chromium needs a TCP listener."""

    request_handler = ServiceProxyHandler

    def __init__(self, interface, policy):
        self.policy = policy
        super().__init__(("127.0.0.1", 0), interface)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", action="append", required=True)
    parser.add_argument("--address", required=True)
    parser.add_argument("--interface", required=True)
    args = parser.parse_args()
    authorities, hosts = set(), set()
    for origin in args.origin:
        parts = urlsplit(origin)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.path
            or parts.query
            or parts.fragment
        ):
            raise ValueError("GUI qualification requires HTTPS origins without credentials, path, query or fragment.")
        port = parts.port if parts.port is not None else 443
        authorities.add(f"{parts.hostname}:{port}")
        hosts.add(parts.hostname)
    policy = ServicePolicy.admit("mapped", sorted(authorities), [f"{host}={args.address}" for host in sorted(hosts)])
    require_interface(args.interface)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, args.interface.encode() + b"\0")
    with BrowserProxy(args.interface, policy) as server:
        publish_readiness(Path("/route/port"), f"{server.server_address[1]}\n")
        server.serve_forever()


if __name__ == "__main__":
    main()
