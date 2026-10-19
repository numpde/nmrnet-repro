"""Compare released-model worker exchanges with direct and retained reference results."""

from tools.container_guard import require_container

require_container()

import asyncio
import hashlib
import importlib.metadata
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
from nmrnet_provider.input_geometry import prepare_json
from nmrnet_provider.prepared_input import PreparedJobInput
from nmrnet_provider.result import ResultPlan
from nmrnet_provider.worker_client import PredictionClient
from tools import baseline_prepare

WORKER_SOURCES = (
    "nmrnet_inference/worker.py",
    "nmrnet_inference/predict.py",
    "nmrnet_inference/model.py",
    "nmrnet_inference/release.py",
    "nmrnet_provider/__init__.py",
    "nmrnet_provider/worker_protocol.py",
    "nmrnet_provider/worker_client.py",
    "nmrnet_provider/input_geometry.py",
    "nmrnet_provider/prepared_input.py",
    "nmrnet_provider/smiles.py",
    "nmrnet_provider/native_diagnostics.py",
    "nmrnet_provider/result.py",
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
    def test_released_batch_matches_direct_results_with_geometry_specific_provenance(self):
        """Use unlike synthetic inputs for wiring/parity, not experimental accuracy."""
        requests = (
            b'{"coordinate_unit":"angstrom","atoms":["H","C"],"coordinates":[[1.09,0,0],[0,0,0]]}',
            b'{"coordinate_unit":"angstrom","atoms":["C"],"coordinates":[[0,0,0]]}',
            b'{"coordinate_unit":"angstrom","atoms":["O"],"coordinates":[[0,0,0]]}',
        )
        plan = self.result_plan(requests, "ordered-batch")
        loader = SelectedReleaseModels(
            declaration_path=Path("/input/prediction-models.json"),
            weight_root=Path("/weight"),
            checkpoint_root=Path("/checkpoints"),
        )
        expected = tuple(predict_request(raw, loader.load_dictionary, loader.load_selected_element) for raw in requests)
        actual = asyncio.run(self.worker_prediction(requests, "ordered-batch"))
        self.assertEqual(expected, tuple(item["prediction"] for item in actual))
        self.assertEqual([{"H", "C"}, {"C"}, set()], [set(item["provenance"]["artifacts"]["elements"]) for item in actual])
        self.assertEqual([hashlib.sha256(raw).hexdigest() for raw in requests], [item["request_sha256"] for item in actual])
        self.assertEqual([], actual[2]["private_warnings"])
        content = plan.finish(actual)
        self.assertEqual(
            ["geometry-1", "geometry-2", "geometry-3"], [item["geometry_id"] for item in json.loads(content)["geometries"]]
        )
        self.assertLessEqual(len(content), plan.maximum_result_bytes)
        print(  # noqa: T201 -- Emit only hashes and equality facts from this native qualification.
            "Released batch parity: "
            + json.dumps(
                {
                    "request_sha256": [item["request_sha256"] for item in actual],
                    "result_sha256": hashlib.sha256(content).hexdigest(),
                    "predictions_exactly_equal": True,
                    "geometry_count": len(actual),
                },
                sort_keys=True,
            )
        )

    def result_plan(self, raw_requests, allocation):
        selection = json.loads(read_fixture_receipt(Path("/input/prediction-models.json")))
        artifacts = {
            "dictionary": {field: selection["dictionary"][field] for field in ("archive_sha256", "member", "size", "sha256")},
            "elements": {
                head: {
                    role: {"archive_sha256": selection["checkpoint_archive"]["archive_sha256"], **member}
                    for role, member in pair.items()
                }
                for head, pair in selection["elements"].items()
            },
        }
        return ResultPlan(
            PreparedJobInput(tuple(prepare_json(raw) for raw in raw_requests), "direct_coordinate_json"),
            job_ref="job:" + allocation,
            execution_attempt_ref="execution_attempt:sha256:" + hashlib.sha256(allocation.encode()).hexdigest(),
            input_fingerprint="sha256:"
            + hashlib.sha256(json.dumps([hashlib.sha256(raw).hexdigest() for raw in raw_requests]).encode()).hexdigest(),
            execution_identity=baseline_prepare.execution_identity(
                image_id=os.environ["NMRNET_QUALIFICATION_IMAGE_ID"],
                repository_revision=os.environ["NMRNET_QUALIFICATION_REPOSITORY_REVISION"],
                tool_sources=[(name, Path("/workspace") / name) for name in WORKER_SOURCES],
            ),
            selected_artifacts=artifacts,
            runtime_expectations={
                "python": sys.version.split()[0],
                "installed_distributions": {
                    name: importlib.metadata.version(name) for name in ("torch", "numpy", "scikit-learn", "joblib")
                },
            },
        )

    def test_no_eligible_atoms_reports_dictionary_but_no_loaded_heads(self):
        raw = b'{"coordinate_unit":"angstrom","atoms":["O"],"coordinates":[[0,0,0]]}'
        plan = self.result_plan((raw,), "no-eligible-heads")
        (result,) = asyncio.run(self.worker_prediction((raw,), "no-eligible-heads"))
        self.assertEqual(json.loads(plan.finish((result,)))["geometries"][0]["predictions"], [])
        self.assertEqual(result["prediction"], {"unit": "ppm", "predictions": []})
        self.assertIsNotNone(result["provenance"]["artifacts"]["dictionary"])
        self.assertEqual(result["provenance"]["artifacts"]["elements"], {})
        self.assertEqual(result["private_warnings"], [])

    async def worker_prediction(self, raw_requests, allocation):
        parent, child = socket.socketpair()
        worker = multiprocessing.get_context("spawn").Process(target=released_worker, args=(child, allocation))
        writer = None
        try:
            worker.start()
            child.close()
            reader, writer = await asyncio.open_unix_connection(sock=parent)
            client = PredictionClient(reader, writer, allocation=allocation, operation_seconds=120)
            prepared = await client.prepare(raw_requests)
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
        selection = json.loads(read_fixture_receipt(Path("/input/prediction-models.json")))
        compared_positions = set()
        for index, (element, key, request) in enumerate(requests):
            raw = json.dumps(request, allow_nan=False).encode()
            plan = self.result_plan((raw,), f"fixture-{index}")
            direct = predict_request(raw, loader.load_dictionary, loader.load_selected_element)
            (worker_result,) = asyncio.run(self.worker_prediction((raw,), f"fixture-{index}"))
            self.assertEqual(set(worker_result), {"request_sha256", "prediction", "provenance", "private_warnings"})
            result_bytes = plan.finish((worker_result,))
            result = json.loads(result_bytes)
            self.assertEqual(result["geometries"][0]["prepared_request"], request)
            self.assertEqual(result["geometries"][0]["predictions"], direct["predictions"])
            self.assertLessEqual(len(result_bytes), plan.maximum_result_bytes)
            actual = worker_result["prediction"]
            self.assertEqual(actual, direct)
            artifacts_seen = worker_result["provenance"]["artifacts"]
            self.assertEqual(
                artifacts_seen["dictionary"],
                {field: selection["dictionary"][field] for field in ("archive_sha256", "member", "size", "sha256")},
            )
            self.assertEqual(set(artifacts_seen["elements"]), set(request["atoms"]) & {"H", "C"})
            for head, pair in artifacts_seen["elements"].items():
                self.assertEqual(
                    pair,
                    {
                        role: {
                            "archive_sha256": selection["checkpoint_archive"]["archive_sha256"],
                            **selection["elements"][head][role],
                        }
                        for role in ("checkpoint", "scaler")
                    },
                )
            runtime = worker_result["provenance"]["runtime"]
            self.assertEqual(runtime["installed_distributions"]["scikit-learn"], "1.8.0")
            self.assertEqual(runtime["python"], sys.version.split()[0])
            self.assertTrue(
                any(
                    item["category"] == "sklearn.exceptions.InconsistentVersionWarning"
                    and item["versions"] == {"original": "1.5.2", "current": "1.8.0"}
                    and item["head"] in {"H", "C"}
                    for item in worker_result["private_warnings"]
                )
            )
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
                    "worker_provenance": worker_result["provenance"],
                    # Raw warning messages/paths stay on the private transport;
                    # the public qualification records classes and the admitted
                    # head/version projection, never those raw strings.
                    "warning_categories": sorted({item["category"] for item in worker_result["private_warnings"]}),
                    "public_warnings": json.loads(result_bytes)["warnings"],
                    "scientific_result": {
                        "size": len(result_bytes),
                        "sha256": hashlib.sha256(result_bytes).hexdigest(),
                        "reserved_bytes": plan.maximum_result_bytes,
                    },
                }
            )
        self.assertEqual(len(compared_positions), 64)
        evidence = {
            "scope": "Released-model parity across spawned worker/socket boundaries; no container-retirement claim.",
            "execution": baseline_prepare.execution_identity(
                image_id=os.environ["NMRNET_QUALIFICATION_IMAGE_ID"],
                repository_revision=os.environ["NMRNET_QUALIFICATION_REPOSITORY_REVISION"],
                tool_sources=[(name, Path("/workspace") / name) for name in WORKER_SOURCES],
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
