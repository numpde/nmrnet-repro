"""Interface binding is mandatory for every proxy-owned outbound socket."""

import socket
import sys
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import bound_http_proxy


class BoundProxyTests(unittest.TestCase):
    def test_connect_binds_before_connecting(self):
        outbound = MagicMock()
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))
        with patch.object(socket, "getaddrinfo", return_value=[address]), patch.object(
            socket, "socket", return_value=outbound
        ):
            self.assertIs(bound_http_proxy.connect_bound("example.invalid", 443, "lo"), outbound)
        self.assertEqual(outbound.method_calls[0].args, (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"lo\0"))
        outbound.connect.assert_called_once_with(("192.0.2.1", 443))

    def test_bind_failure_never_connects_or_falls_back(self):
        outbound = MagicMock()
        outbound.setsockopt.side_effect = PermissionError("binding denied")
        address = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443))
        with patch.object(socket, "getaddrinfo", return_value=[address]), patch.object(
            socket, "socket", return_value=outbound
        ), self.assertRaises(PermissionError):
            bound_http_proxy.connect_bound("example.invalid", 443, "lo")
        outbound.connect.assert_not_called()
        outbound.close.assert_called_once_with()
