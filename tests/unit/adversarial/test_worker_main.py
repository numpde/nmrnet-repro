"""Startup refusal cannot adopt old grants or reach scientific imports."""

from tools.container_guard import require_container

require_container()

import builtins
import io
import socket
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from nmrnet_provider import worker_main

COMMON = ["listener", "--accept-seconds", "1", "--io-seconds", "2"]


class WorkerMainAdversarialTests(unittest.TestCase):
    def test_bad_allocation_deadlines_or_role_settings_fail_before_listening(self):
        cases = [
            ["reader", "../foreign", *COMMON[1:]],
            ["reader", *COMMON, "--accept-seconds", "nan"],
            ["reader", *COMMON, "--io-seconds", "0"],
            ["reader", *COMMON, "--seed", "17"],
            ["smiles", *COMMON],
            ["smiles", *COMMON, "--seed", "17", "--max-iterations", "80", "--embedding-seconds", "2"],
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments), patch.object(socket, "socket") as acquire, redirect_stderr(io.StringIO()):
                with self.assertRaises((ValueError, SystemExit)):
                    worker_main.main(arguments)
                acquire.assert_not_called()

    def test_mismatched_declaration_refuses_before_prediction_import_or_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("ipc", "input", "embedded"):
                (root / name).mkdir()
            original_import = builtins.__import__

            def deny_science(name, *arguments, **keywords):
                if name == "nmrnet_inference.worker" or name.split(".")[0] in ("torch", "rdkit"):
                    self.fail("Scientific wiring imported before declaration admission")
                return original_import(name, *arguments, **keywords)

            for mismatched in ("inference.lock", "unicore-source.json"):
                for name in ("inference.lock", "unicore-source.json"):
                    (root / "input" / name).write_bytes(b"mounted\n")
                    (root / "embedded" / name).write_bytes(b"other\n" if name == mismatched else b"mounted\n")
                with (
                    self.subTest(mismatched=mismatched),
                    patch.object(worker_main, "IPC_ROOT", root / "ipc"),
                    patch.object(worker_main, "INPUT_ROOT", root / "input"),
                    patch.object(worker_main, "INFERENCE_ROOT", root / "embedded"),
                    patch("builtins.__import__", side_effect=deny_science),
                    patch.object(socket, "socket") as acquire,
                    self.assertRaisesRegex(ValueError, mismatched + ".*differs.*rebuild"),
                ):
                    worker_main.main(["prediction", *COMMON])
                acquire.assert_not_called()
                self.assertEqual(list((root / "ipc").iterdir()), [])

    def test_stale_ready_or_socket_is_preserved_and_never_adopted(self):
        for name in ("ready", "geometry.sock"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                stale = root / name
                stale.write_bytes(b"original-host-evidence")
                with patch.object(worker_main, "IPC_ROOT", root), self.assertRaises(OSError):
                    worker_main.main(["reader", *COMMON])
                self.assertEqual(stale.read_bytes(), b"original-host-evidence")

    def test_accept_timeout_leaves_readiness_and_socket_for_host_retirement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(worker_main, "IPC_ROOT", root), self.assertRaises(TimeoutError):
                worker_main.main(["reader", *COMMON, "--accept-seconds", "0.01"])
            self.assertEqual((root / "ready").read_bytes(), b"listener\n")
            self.assertTrue((root / "geometry.sock").is_socket())
