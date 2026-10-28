"""Runner composition preserves fixed role policy and scientific startup facts."""

from tools.container_guard import require_container

require_container()

import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nmrnet_provider import native_child, worker_main
from nmrnet_provider.worker_protocol import receive

COMMON = ["--socket", "/runtime/runner.sock", "--scratch", "/tmp/operations",
          "--io-seconds", "2", "--operation-seconds", "10", "--startup-seconds", "1",
          "--terminate-seconds", "1", "--kill-seconds", "1", "--cleanup-seconds", "1"]


class RuntimeLoggingTests(unittest.TestCase):
    def test_owned_verbosity_dependency_warning_and_stream_identity(self):
        script = """
import logging, sys
from nmrnet_provider.runtime_logging import configure_logging
original = sys.stdout
configure_logging(sys.argv[1])
assert sys.stdout is original
assert len(logging.getLogger().handlers) == 1
logging.getLogger('nmrnet_provider.worker_main').info('owned-info')
logging.getLogger('nmrnet_inference.worker').debug('owned-debug')
logging.getLogger('provider_core.runner').info('runner-info')
logging.getLogger('dependency').setLevel(logging.DEBUG)
logging.getLogger('dependency').info('dependency-info')
logging.getLogger('dependency').debug('dependency-debug')
logging.getLogger('dependency').warning('dependency-warning')
"""
        for level in ("INFO", "DEBUG", "WARNING"):
            with self.subTest(level=level):
                result = subprocess.run(
                    [sys.executable, "-B", "-c", script, level], capture_output=True, text=True, timeout=5, check=False
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")
                self.assertEqual(result.stdout.count("owned-info"), int(level != "WARNING"))
                self.assertEqual(result.stdout.count("owned-debug"), int(level == "DEBUG"))
                self.assertEqual(result.stdout.count("runner-info"), int(level != "WARNING"))
                self.assertNotIn("dependency-info", result.stdout)
                self.assertNotIn("dependency-debug", result.stdout)
                self.assertEqual(result.stdout.count("dependency-warning"), 1)

    def test_invalid_verbosity_refuses_before_installing_a_handler(self):
        script = """
import logging
from nmrnet_provider.runtime_logging import configure_logging
try:
    configure_logging('TRACE')
except ValueError:
    assert not logging.getLogger().handlers
else:
    raise AssertionError('Invalid verbosity admitted')
"""
        result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), (b"", b""))


class RunnerCompositionTests(unittest.TestCase):
    def test_reader_supplies_one_fixed_isolated_child_command(self):
        with patch.object(worker_main, "supervise") as supervise:
            worker_main.main(["reader", *COMMON])
        (arguments,) = supervise.call_args.args
        split = arguments.index("--")
        self.assertEqual(arguments[split + 1:], [
            sys.executable, "-I", "-B", "-m", "nmrnet_provider.native_child", "reader", "--io-seconds", "2.0",
        ])
        self.assertEqual(arguments[arguments.index("--operation-seconds") + 1], "10.0")
        self.assertEqual(arguments[arguments.index("--role") + 1], "reader")

    def test_generator_receives_only_its_explicit_settings(self):
        with patch.object(worker_main, "supervise") as supervise:
            worker_main.main(["smiles", *COMMON, "--seed", "17", "--max-iterations", "80", "--embedding-seconds", "1"])
        (arguments,) = supervise.call_args.args
        self.assertEqual(arguments[-6:], ["--seed", "17", "--max-iterations", "80", "--embedding-seconds", "1"])

    def test_prediction_emits_observed_runtime_before_serving_without_a_metadata_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for leaf in ("input", "embedded"):
                (root / leaf).mkdir()
                for name in ("inference.lock", "unicore-source.json"):
                    (root / leaf / name).write_bytes((name + " exact\n").encode())
            sender, receiver = socket.socketpair()
            with (
                sender, receiver,
                patch.object(native_child, "INPUT_ROOT", root / "input"),
                patch.object(native_child, "INFERENCE_ROOT", root / "embedded"),
                patch("importlib.metadata.version", side_effect=lambda name: name + "-observed"),
                patch("nmrnet_inference.worker.serve_released_connection") as serve,
            ):
                native_child._prediction(SimpleNamespace(operation="a" * 32, io_seconds=2), sender)
                startup = receive(receiver, 2)
            self.assertEqual(startup, {
                "kind": "startup", "allocation": "a" * 32,
                "runtime": {"python": sys.version.split()[0], "installed_distributions": {
                    name: name + "-observed" for name in ("torch", "numpy", "scikit-learn", "joblib")
                }},
            })
            self.assertEqual(serve.call_args.kwargs["allocation"], "a" * 32)
            self.assertEqual(serve.call_args.kwargs["declaration_path"], root / "input/prediction-models.json")
            self.assertFalse((root / "metadata.json").exists())
