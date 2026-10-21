"""Service proxy admits explicit routes without changing destination TLS identity."""

from tools.container_guard import require_container

require_container()

import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import bound_http_proxy, provider_proxy
from tools.provider_proxy import ServicePolicy, ServiceProxyServer


class ServiceProxyTests(unittest.TestCase):
    def test_mapped_publication_preserves_authority_and_port_without_resolving_the_name(self):
        policy = ServicePolicy.admit("mapped", ["nmr.localhost:10443"], ["nmr.localhost=172.17.0.1"])
        self.assertEqual(
            policy.destination(b"CONNECT nmr.localhost:10443 HTTP/1.1\r\n\r\n")[:3],
            ("CONNECT", "nmr.localhost", 10443),
        )
        with patch.object(bound_http_proxy, "_dns_servers") as dns, patch.object(socket, "socket") as factory:
            policy.connect("nmr.localhost", 10443, "lo")
        dns.assert_not_called()
        factory.return_value.setsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"lo\0")
        factory.return_value.connect.assert_called_once_with(("172.17.0.1", 10443))

    def test_cli_publishes_private_complete_readiness_only_after_socket_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            path, ready = Path(directory) / "proxy.sock", Path(directory) / "ready"
            argv = [
                "provider_proxy",
                "--interface",
                "lo",
                "--mode",
                "external",
                "--authority",
                "api.example:443",
                "--socket",
                str(path),
                "--ready-file",
                str(ready),
            ]

            def observe_ready(_server):
                self.assertEqual(ready.read_bytes(), b"ready\n")
                self.assertEqual(ready.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

            with patch.object(provider_proxy.sys, "argv", argv), patch.object(ServiceProxyServer, "serve_forever", observe_ready):
                provider_proxy.main()

    def test_local_mapping_selects_only_the_frozen_address_without_dns(self):
        policy = ServicePolicy.admit("local", ["api.example:443", "api.example:8443"], ["api.example=192.0.2.8"])
        sentinel = object()
        with patch.object(bound_http_proxy, "connect_bound", return_value=sentinel) as connect:
            self.assertIs(policy.connect("api.example", 443, "eth0"), sentinel)
        connect.assert_called_once_with("192.0.2.8", 443, "eth0")
        self.assertEqual(policy.destination(b"CONNECT api.example:8443 HTTP/1.1\r\n\r\n")[:3], ("CONNECT", "api.example", 8443))

    def test_external_route_preserves_the_admitted_host_for_bound_resolution(self):
        policy = ServicePolicy.admit("external", ["model.example:443"], [])
        with patch.object(bound_http_proxy, "connect_bound") as connect:
            policy.connect("model.example", 443, "wlan0")
        connect.assert_called_once_with("model.example", 443, "wlan0")

    def test_real_unix_listener_relays_bytes_and_preserves_client_half_close(self):
        policy = ServicePolicy.admit("local", ["api.example:443"], ["api.example=127.0.0.1"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.sock"
            outbound, peer = socket.socketpair()
            with patch.object(bound_http_proxy, "connect_bound", return_value=outbound) as connect:
                with ServiceProxyServer(path, "lo", policy) as server:
                    worker = threading.Thread(target=server.handle_request)
                    worker.start()
                    try:
                        with socket.socket(socket.AF_UNIX) as client, peer:
                            client.settimeout(2)
                            peer.settimeout(2)
                            client.connect(str(path))
                            client.sendall(b"CONNECT api.example:443 HTTP/1.1\r\n\r\nopaque-prefix")
                            self.assertEqual(client.recv(4096), b"HTTP/1.1 200 Connection Established\r\n\r\n")
                            self.assertEqual(peer.recv(4096), b"opaque-prefix")
                            client.shutdown(socket.SHUT_WR)
                            self.assertEqual(peer.recv(4096), b"")
                            peer.sendall(b"opaque-response")
                            peer.shutdown(socket.SHUT_WR)
                            self.assertEqual(client.recv(4096), b"opaque-response")
                            self.assertEqual(client.recv(4096), b"")
                    finally:
                        worker.join(2)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                connect.assert_called_once_with("127.0.0.1", 443, "lo")
