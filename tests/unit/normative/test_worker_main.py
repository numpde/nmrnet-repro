"""Production listeners use the existing one-use scientific channels."""

from tools.container_guard import require_container

require_container()

import asyncio
import json
import multiprocessing
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from nmrnet_provider import worker_main
from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.smiles import GenerationSettings

ALLOCATION = "production-listener"
COMMON = [ALLOCATION, "--accept-seconds", "2", "--io-seconds", "2"]


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


def run_reader(directory):
    worker_main.IPC_ROOT = Path(directory)
    worker_main.DIAGNOSTICS_ROOT = Path(directory)
    worker_main.main(["reader", *COMMON])


class WorkerMainNormativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_reader_serves_exact_source_once_and_leaves_grants_to_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = multiprocessing.get_context("fork").Process(target=run_reader, args=(directory,))
            process.start()
            client = None
            try:
                async with asyncio.timeout(3):
                    while not (root / "ready").exists():
                        if not process.is_alive():
                            self.fail("Reader exited before publishing channel readiness")
                        await asyncio.sleep(0.01)
                    self.assertEqual((root / "ready").read_bytes(), (ALLOCATION + "\n").encode())
                    reader, writer = await asyncio.open_unix_connection(str(root / "geometry.sock"))
                    client = GeometryClient(reader, writer, allocation=ALLOCATION, operation_seconds=2)
                    (geometry,) = await client.read(
                        b"H 0 0 0\nC 1.09 0 0\n", source_format="table", source_unit="angstrom", selection=None
                    )
                    self.assertEqual(json.loads(geometry.request_bytes)["atoms"], ["H", "C"])
                    self.assertEqual(json.loads(geometry.request_bytes)["coordinates"], [[0, 0, 0], [1.09, 0, 0]])
                    self.assertEqual(geometry.source_positions, (1, 2))
            finally:
                if client is not None:
                    client.close()
                    await client.wait_closed()
                await asyncio.to_thread(process.join, 3)
                if process.is_alive():
                    process.kill()
                    await asyncio.to_thread(process.join)
            self.assertEqual(process.exitcode, 0)
            self.assertTrue((root / "geometry.sock").is_socket())
            self.assertTrue((root / "ready").exists())
            resources = json.loads((root / "resources.json").read_bytes())
            self.assertEqual((resources["allocation"], resources["role"]), (ALLOCATION, "reader"))
            self.assertEqual(resources["scope"], "cgroup_through_final_snapshot")
            self.assertGreater(int(resources["cgroup"]["memory.peak"]), 0)
            self.assertFalse((root / "resources.pending").exists())

    def test_generator_receives_only_the_explicit_host_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listener, connection = Mock(), Mock()
            listener.__enter__ = Mock(return_value=listener)
            listener.__exit__ = Mock(return_value=False)
            connection.__enter__ = Mock(return_value=connection)
            connection.__exit__ = Mock(return_value=False)
            listener.accept.return_value = connection, None
            with (
                patch.object(worker_main, "IPC_ROOT", root),
                patch.object(worker_main, "DIAGNOSTICS_ROOT", root),
                patch.object(socket, "socket", return_value=listener),
                patch("nmrnet_provider.smiles_worker.serve_smiles_connection") as serve,
            ):
                worker_main.main(["smiles", *COMMON, "--seed", "17", "--max-iterations", "80", "--embedding-seconds", "1"])
            serve.assert_called_once_with(connection, allocation=ALLOCATION, settings=GenerationSettings(17, 80, 1), io_seconds=2)
            listener.bind.assert_called_once_with(str(root / "geometry.sock"))
            listener.settimeout.assert_called_once_with(2)

    def test_prediction_admits_embedded_declarations_and_publishes_observed_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for subdirectory in ("ipc", "input", "embedded"):
                (root / subdirectory).mkdir()
            for name in ("inference.lock", "unicore-source.json"):
                for subdirectory in ("input", "embedded"):
                    (root / subdirectory / name).write_bytes((name + " exact\n").encode())
            with (
                patch.object(worker_main, "IPC_ROOT", root / "ipc"),
                patch.object(worker_main, "DIAGNOSTICS_ROOT", root),
                patch.object(worker_main, "INPUT_ROOT", root / "input"),
                patch.object(worker_main, "INFERENCE_ROOT", root / "embedded"),
                patch.object(worker_main, "_listen") as listen,
                patch(
                    "importlib.metadata.version",
                    side_effect=["torch-version", "numpy-version", "sklearn-version", "joblib-version"],
                ),
                patch("nmrnet_inference.worker.serve_released_connection") as serve,
            ):
                worker_main.main(["prediction", *COMMON])
                handler = listen.call_args.kwargs["handler"]
                connection = object()
                handler(connection)
            serve.assert_called_once_with(
                connection,
                allocation=ALLOCATION,
                io_seconds=2,
                declaration_path=root / "input/prediction-models.json",
                weight_root=Path("/weight"),
                checkpoint_root=Path("/checkpoints"),
            )
            observed = json.loads((root / "ipc/metadata.json").read_bytes())
            self.assertEqual(
                observed["runtime"]["installed_distributions"],
                {
                    "torch": "torch-version",
                    "numpy": "numpy-version",
                    "scikit-learn": "sklearn-version",
                    "joblib": "joblib-version",
                },
            )
