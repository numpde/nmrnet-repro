"""Compare released-model worker exchanges with direct and retained reference results."""

from tools.container_guard import require_container

require_container()

import asyncio
import hashlib
import json
import multiprocessing
import os
import socket
import sys
import unittest
from pathlib import Path

if os.environ.get("NMRNET_PREDICTION_PARITY") != "1":
    raise unittest.SkipTest("run make prediction/worker-parity")

import test_prediction_release as release_reference
from test_prediction_model import EXPECTED_REFERENCE_PACKET_SHA256, read_fixture_receipt

from nmrnet_inference.predict import predict_request
from nmrnet_inference.release import SelectedReleaseModels
from nmrnet_inference.worker import serve_released_connection
from nmrnet_provider.worker_client import PredictionClient
from tools import baseline_prepare


def released_worker(connection, allocation):
    with connection:
        serve_released_connection(
            connection,
            allocation=allocation,
            io_seconds=60,
            declaration_path=Path("/input/prediction-models.json"),
            weight_root=Path("/weight"),
            checkpoint_root=Path("/checkpoints"),
        )


class ReleasedWorkerTests(unittest.TestCase):
    async def worker_prediction(self, raw, allocation):
        parent, child = socket.socketpair()
        worker = multiprocessing.get_context("spawn").Process(target=released_worker, args=(child, allocation))
        writer = None
        try:
            worker.start()
            child.close()
            reader, writer = await asyncio.open_unix_connection(sock=parent)
            client = PredictionClient(reader, writer, allocation=allocation, operation_seconds=120)
            prepared = await client.prepare(raw)
            return await client.execute(prepared)
        finally:
            try:
                if writer is not None:
                    writer.close()
                    async with asyncio.timeout(1):
                        await writer.wait_closed()
            finally:
                child.close()
                parent.close()
                if worker.pid is not None:
                    await asyncio.to_thread(worker.join, 5)
                    forced = worker.is_alive()
                    if forced:
                        worker.kill()
                        await asyncio.to_thread(worker.join, 5)
                    self.assertFalse(worker.is_alive(), "Scientific qualification worker did not stop")
                    exitcode = worker.exitcode
                    worker.close()
                    self.assertFalse(forced, "Scientific qualification worker required forced cleanup")
                    self.assertEqual(exitcode, 0)

    def test_four_released_worker_results_match_direct_and_reference(self):
        reference_bytes = read_fixture_receipt(Path("/input/reference-cpu-float32.json"))
        self.assertEqual(hashlib.sha256(reference_bytes).hexdigest(), EXPECTED_REFERENCE_PACKET_SHA256)
        reference = json.loads(reference_bytes)
        self.assertEqual(os.environ["NMRNET_QUALIFICATION_UPSTREAM_REVISION"], reference["provenance"]["upstream_revision"])
        requests = release_reference.reference_requests(self, reference)
        self.assertEqual(len({(element, key) for element, key, _ in requests}), 4)
        loader = SelectedReleaseModels(
            declaration_path=Path("/input/prediction-models.json"),
            weight_root=Path("/weight"),
            checkpoint_root=Path("/checkpoints"),
        )
        observations = []
        compared_positions = set()
        for index, (element, key, request) in enumerate(requests):
            raw = json.dumps(request, allow_nan=False).encode()
            direct = predict_request(raw, loader.load_dictionary, loader.load_selected_element)
            actual = asyncio.run(self.worker_prediction(raw, f"fixture-{index}"))
            self.assertEqual(actual, direct)
            self.assertEqual(actual["unit"], "ppm")
            self.assertEqual(
                [row["atom_index"] for row in actual["predictions"]],
                [i for i, atom in enumerate(request["atoms"]) if atom in ("H", "C")],
            )
            values = {row["atom_index"]: row["shift_ppm"] for row in actual["predictions"]}
            for expected in reference["elements"][element]["predictions"]:
                if expected["key_hex"] == key:
                    self.assertEqual(values[expected["atom_index"]], expected["prediction_ppm"])
                    compared_positions.add((element, key, expected["atom_index"]))
            observations.append(
                {
                    "source_element": element,
                    "source_key_hex": key,
                    "request": {"size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
                    "prediction_count": len(actual["predictions"]),
                    "result_sha256": hashlib.sha256(json.dumps(actual, sort_keys=True, allow_nan=False).encode()).hexdigest(),
                }
            )
        self.assertEqual(len(compared_positions), 64)
        sources = (
            "nmrnet_inference/worker.py",
            "nmrnet_inference/predict.py",
            "nmrnet_inference/model.py",
            "nmrnet_inference/release.py",
            "nmrnet_provider/worker_protocol.py",
            "nmrnet_provider/worker_client.py",
            "tests/test_prediction_worker_release.py",
            "tests/test_prediction_release.py",
            "tests/test_prediction_model.py",
            "tools/container_guard.py",
            "tools/artifacts.py",
            "tools/baseline_prepare.py",
            "tools/baseline_model.py",
            "tools/baseline.sh",
            "tools/inference_native.sh",
            "tools/docker_client.sh",
            "tools/git_metadata.sh",
        )
        evidence = {
            "scope": "Released-model parity across spawned worker/socket boundaries; no container-retirement claim.",
            "execution": baseline_prepare.execution_identity(
                image_id=os.environ["NMRNET_QUALIFICATION_IMAGE_ID"],
                repository_revision=os.environ["NMRNET_QUALIFICATION_REPOSITORY_REVISION"],
                tool_sources=[(name, Path("/workspace") / name) for name in sources],
            ),
            "reference_packet_sha256": EXPECTED_REFERENCE_PACKET_SHA256,
            "resources": release_reference.ReleasedPredictionTests.assert_observed_container_budget(self),
            "selection": json.loads(read_fixture_receipt(Path("/input/prediction-models.json"))),
            "atol": 0,
            "rtol": 0,
            "reference_positions": 64,
            "fixtures": observations,
        }
        sys.stdout.write("NMRNET_WORKER_OBSERVATION " + json.dumps(evidence, sort_keys=True, allow_nan=False) + "\n")
        sys.stdout.flush()
