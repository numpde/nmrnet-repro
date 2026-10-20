"""Production listeners use the existing one-use scientific channels."""

from tools.container_guard import require_container

require_container()

import asyncio
import json
import multiprocessing
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from nmrnet_provider import worker_main
from nmrnet_provider.geometry_client import GeometryClient
from nmrnet_provider.smiles import GenerationSettings

ALLOCATION = "production-listener"
COMMON = [ALLOCATION, "--accept-seconds", "2", "--io-seconds", "2"]


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
