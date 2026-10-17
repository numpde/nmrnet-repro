"""Execute released models on pinned fixtures without qualifying physical units."""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

if os.environ.get("NMRNET_PREDICTION_PARITY") != "1":
    raise unittest.SkipTest("run make prediction/model-parity")

import lmdb
import numpy as np
import torch

from nmrnet_inference import model as prediction_model, predict
from test_prediction_model import (
    EXPECTED_REFERENCE_PACKET_SHA256,
    PARITY_TEST_UNIT,
    load_reference_pickle,
    read_fixture_receipt,
    read_receipted_bytes,
)
from tools import artifacts, baseline_model, baseline_prepare


def reference_requests(test, reference):
    """Keep source labels in the oracle; production requests contain none."""
    receipt_bytes = read_fixture_receipt(Path("/prepared/provenance.json"))
    identity = reference["evidence_identities"]["prepared_receipt"]
    test.assertEqual(len(receipt_bytes), identity["size"])
    test.assertEqual(hashlib.sha256(receipt_bytes).hexdigest(), identity["sha256"])
    receipt = json.loads(receipt_bytes)
    requests = []
    with tempfile.TemporaryDirectory(prefix="nmrnet-release-fixtures-") as temporary:
        for element in ("H", "C"):
            root = Path("/prepared") / element
            records = reference["elements"][element]["records"]
            retained_records = receipt["elements"][element]["records"]
            test.assertEqual(len(retained_records), len(records))
            for actual, expected in zip(retained_records, records):
                test.assertEqual({name: actual[name] for name in expected}, expected)
            files = receipt["elements"][element]["files"]
            keys = load_reference_pickle(read_receipted_bytes(root / "valid_dbid.pkl", files["valid_dbid.pkl"]))
            test.assertEqual([key.hex() for key in keys], [record["key_hex"] for record in records])
            database = Path(temporary) / (element + ".lmdb")
            database.write_bytes(read_receipted_bytes(root / "valid.lmdb", files["valid.lmdb"]))
            with lmdb.open(str(database), subdir=False, readonly=True, lock=False) as environment:
                with environment.begin() as transaction:
                    test.assertEqual(transaction.stat()["entries"], len(records))
                    for key, expected in zip(keys, records):
                        content = transaction.get(key)
                        test.assertIsNotNone(content)
                        test.assertEqual(hashlib.sha256(content).hexdigest(), expected["raw_sha256"])
                        record = load_reference_pickle(content)
                        requests.append((element, key.hex(), {
                            "coordinate_unit": PARITY_TEST_UNIT,
                            "atoms": list(record["atoms"]),
                            "coordinates": np.asarray(record["coordinates"]).tolist(),
                        }))
    test.assertEqual(len(requests), 4)
    return requests


class ReleasedPredictionTests(unittest.TestCase):
    def setUp(self):
        content = read_fixture_receipt(Path("/input/reference-cpu-float32.json"))
        self.assertEqual(hashlib.sha256(content).hexdigest(), EXPECTED_REFERENCE_PACKET_SHA256)
        self.reference = json.loads(content)
        self.release = artifacts.load_declaration(Path("/input/release.json"))

    def verified_bytes(self, root, archive_name, expected):
        archive = next(item for item in self.release["files"] if item["name"] == archive_name)
        selection_name = "weight.selection.json" if archive_name == "weight.zip" else "checkpoints.selection.json"
        selection = json.loads(read_fixture_receipt(Path("/input") / selection_name))
        self.assertIn(expected["member"], selection["members"])
        self.assertLessEqual(expected["size"], selection["max_bytes"])
        content, identity = baseline_prepare.verified_member_bytes(
            Path(root), archive, {"members": [expected["member"]], "max_bytes": expected["size"]}
        )
        self.assertEqual(
            {key: identity[key] for key in ("member", "size", "sha256")},
            {key: expected[key] for key in ("member", "size", "sha256")},
        )
        return content

    def assert_baseline_configuration(self, model, element):
        """Compare real model args with the independently parsed upstream invocation."""
        expected = self.reference["elements"][element]
        paths = {role: Path("/checkpoints") / item["member"] for role, item in expected["artifacts"].items()}
        argv = baseline_model.inference_argv(
            Path("/prepared"), element, paths,
            {"max_atoms": 128, "max_tokens": 130}, Path("/tmp"), Path("/opt/nmrnet/uninmr"),
        )
        # Uni-Core rejects registering a user module already imported outside its
        # own importer. Resolve the independent upstream CLI in a fresh process,
        # just as the baseline script does; all execution stays in this container.
        program = """
import json
import sys
from pathlib import Path
from unicore import options
output = Path(sys.argv.pop(1))
parser = options.get_validation_parser()
parser.add_argument('--subset_name', type=str)
options.add_model_args(parser)
args = options.parse_args_and_arch(parser, input_args=sys.argv[1:])
output.write_text(json.dumps(vars(args), allow_nan=False))
"""
        with tempfile.TemporaryDirectory(prefix="nmrnet-model-config-") as temporary:
            output = Path(temporary) / "args.json"
            result = subprocess.run(
                [sys.executable, "-B", "-c", program, str(output), *argv],
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
            resolved_args = json.loads(read_fixture_receipt(output))
        for name, value in vars(model.args).items():
            self.assertEqual(value, resolved_args[name], name)
        self.assertEqual(
            prediction_model.validate_model(model, torch.float32), expected["model"]["parameter_count"]
        )
        self.assertFalse(model.training)

    def assert_synthetic_capacity(self, model, dictionary, scaler, element):
        """Exercise tensor capacity only; these artificial coordinates prove no chemistry."""
        for count in (1, predict.MAX_ATOMS):
            request = {
                "coordinate_unit": PARITY_TEST_UNIT,
                "atoms": [element] * count,
                "coordinates": [[index * 0.1, 0.0, 0.0] for index in range(count)],
            }
            with self.subTest(element=element, synthetic_atom_count=count):
                predictions = prediction_model.predict_element(request, element, model, dictionary, scaler)
                self.assertEqual([index for index, _value in predictions], list(range(count)))
        absent = {"coordinate_unit": PARITY_TEST_UNIT, "atoms": ["O"], "coordinates": [[0.0, 0.0, 0.0]]}
        self.assertEqual(prediction_model.predict_element(absent, element, model, dictionary, scaler), ())

    def assert_observed_container_budget(self):
        facts = {
            name: (Path("/sys/fs/cgroup") / name).read_text().strip()
            for name in ("cpu.max", "memory.max", "memory.peak", "memory.swap.max", "pids.max")
        }
        self.assertEqual(facts["cpu.max"], "200000 100000")
        self.assertEqual(facts["memory.max"], str(4 * 1024**3))
        self.assertEqual(facts["memory.swap.max"], "0")
        self.assertEqual(facts["pids.max"], "64")
        self.assertGreater(int(facts["memory.peak"]), 0)
        self.assertLessEqual(int(facts["memory.peak"]), int(facts["memory.max"]))
        print("Observed release-model qualification resources: " + json.dumps(facts, sort_keys=True))

    def test_real_models_match_all_64_reference_positions_without_task_setup(self):
        requests = reference_requests(self, self.reference)
        dictionary_bytes = self.verified_bytes(
            "/weight", "weight.zip", self.reference["source_artifacts"]["dictionary"]
        )
        dictionary = prediction_model.load_prediction_dictionary_from_bytes(dictionary_bytes)
        self.assertEqual(dictionary.symbols, self.reference["dictionary"]["symbols"])
        self.assertEqual(dictionary.indices["[MASK]"], 30)
        observed = set()
        with (
            mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": PARITY_TEST_UNIT, "C": PARITY_TEST_UNIT}),
            mock.patch("unicore.tasks.setup_task", side_effect=AssertionError("prediction must not set up a task")),
            mock.patch("uninmr.tasks.uninmr.UniNMRTask.__init__", side_effect=AssertionError("prediction must not construct a task")),
        ):
            for element in ("H", "C"):
                expected = self.reference["elements"][element]
                checkpoint = self.verified_bytes("/checkpoints", "checkpoints.zip", expected["artifacts"]["checkpoint"])
                model = prediction_model.load_prediction_model_from_bytes(checkpoint, dictionary)
                del checkpoint
                scaler = prediction_model.load_scaler_from_bytes(
                    self.verified_bytes("/checkpoints", "checkpoints.zip", expected["artifacts"]["scaler"])
                )
                self.assert_baseline_configuration(model, element)
                repeated_outputs = {}
                for fixture_element, key, request in [*requests, *reversed(requests)]:
                    with self.subTest(model=element, fixture=fixture_element, key=key):
                        predictions = dict(prediction_model.predict_element(request, element, model, dictionary, scaler))
                        self.assertEqual(list(predictions), [
                            index for index, atom in enumerate(request["atoms"]) if atom == element
                        ])
                        fixture_id = (fixture_element, key)
                        if fixture_id in repeated_outputs:
                            self.assertEqual(predictions, repeated_outputs[fixture_id])
                        else:
                            repeated_outputs[fixture_id] = predictions
                        if fixture_element == element:
                            for item in expected["predictions"]:
                                if item["key_hex"] == key:
                                    self.assertEqual(predictions[item["atom_index"]], item["prediction_ppm"])
                                    observed.add((element, key, item["atom_index"]))
                self.assert_synthetic_capacity(model, dictionary, scaler, element)
                del model, scaler
        population = {
            (element, item["key_hex"], item["atom_index"])
            for element in ("H", "C") for item in self.reference["elements"][element]["predictions"]
        }
        self.assertEqual(len(population), 64)
        self.assertEqual(observed, population)
        self.assert_observed_container_budget()
