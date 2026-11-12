"""Verify the acquisition owner's configured DNS capture and private output."""

from tools.container_guard import require_container

require_container()

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repository_checks.quality.sources import ROOT


spec = importlib.util.spec_from_file_location("acquisition_dns", ROOT / "host/bootstrap/acquisition/acquisition_tool/dns.py")
dns = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dns)


class ProxyResolverInputTests(unittest.TestCase):
    def test_link_dns_takes_precedence_and_filters_addresses(self):
        with patch.object(dns.socket, "if_nametoindex", return_value=1), patch.object(
            dns, "_manager_output", return_value="127.0.0.1 192.0.2.53 invalid 192.0.2.53 2001:db8::1"
        ) as manager, patch.object(Path, "read_text", side_effect=AssertionError("global DNS consulted")):
            self.assertEqual(("192.0.2.53",), dns.capture_dns_servers("eth0"))
        manager.assert_called_once_with(["nmcli", "--get-values", "IP4.DNS", "device", "show", "eth0"])

    def test_global_dns_uses_configured_sources_without_inventing_servers(self):
        def read(path, **kwargs):
            if str(path) == "/run/systemd/resolve/resolv.conf":
                return "nameserver 127.0.0.53\n"
            return "nameserver 198.51.100.53\nnameserver 198.51.100.53\nnameserver invalid\n"

        with patch.object(dns, "_manager_output", return_value=""), patch.object(
            dns.socket, "if_nametoindex", return_value=1
        ), patch.object(Path, "read_text", read):
            self.assertEqual(("198.51.100.53",), dns.capture_dns_servers("eth0"))

    def test_no_configured_usable_dns_fails(self):
        with patch.object(Path, "read_text", return_value="nameserver 127.0.0.53\n"):
            with self.assertRaisesRegex(dns.ResolverCaptureFailed, "configure an IPv4 resolver"):
                dns.capture_dns_servers("system")

    def test_output_symlink_is_refused_without_changing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.write_text("sentinel\n")
            output = root / "resolv.conf"
            output.symlink_to(target)
            stderr = io.StringIO()
            with patch.object(dns, "capture_dns_servers", return_value=("192.0.2.53",)), patch.object(
                sys, "argv", ["dns.py", "--interface", "system", "--output", str(output)]
            ), contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as exit_status:
                    dns.main()
            self.assertEqual(2, exit_status.exception.code)
            self.assertIn("Cannot prepare acquisition resolver capture", stderr.getvalue())
            self.assertTrue(output.is_symlink())
            self.assertEqual("sentinel\n", target.read_text())
