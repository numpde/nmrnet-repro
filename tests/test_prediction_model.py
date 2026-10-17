"""Compare coordinate-driven tensors with upstream without claiming their physical unit."""

import hashlib
import io
import json
import os
import pickle
import pickletools
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

if os.environ.get("NMRNET_PREDICTION_PARITY") != "1":
    # The ordinary unit image intentionally excludes the scientific stack; this
    # module runs only in the recorded inference-image parity lane.
    raise unittest.SkipTest("run make prediction/tensor-parity")

import numpy as np
import torch
from unicore.data import (
    AppendTokenDataset,
    Dictionary,
    PrependTokenDataset,
    RightPadDataset,
    RightPadDataset2D,
    TokenizeDataset,
)

from nmrnet_inference import predict
from nmrnet_inference.model import (
    compose_net_input,
    load_checkpoint_from_bytes,
    load_dictionary_from_bytes,
    load_scaler_from_bytes,
    predict_element,
)
from tools import artifacts, baseline_prepare
from uninmr.data import (
    CroppingDataset,
    DistanceDataset,
    EdgeTypeDataset,
    KeyDataset,
    NormalizeDataset,
    PrependAndAppend2DDataset,
    RightPadDataset2D0,
    SelectTokenDataset,
    ToTorchDataset,
)


MAX_REFERENCE_FILE_BYTES = 64 * 1024 * 1024
MAX_PROVENANCE_BYTES = 1024 * 1024
PARITY_TEST_UNIT = "tensor-parity-only"
# Pin the test oracle itself; prepared receipts under .work can be replaced.
EXPECTED_REFERENCE_PACKET_SHA256 = "8268206ad12003dea5ef57e1a7392de69325e110b54f2b91ee0f6314c4fef5ae"
FORBIDDEN_PICKLE_OPCODES = {"EXT1", "EXT2", "EXT4", "PERSID", "BINPERSID"}


def compose_for_tensor_parity(atoms, coordinates, dictionary, element):
    """Exercise the public composer using a test-only unit label, not evidence."""
    request = {
        "coordinate_unit": PARITY_TEST_UNIT,
        "atoms": atoms,
        "coordinates": coordinates,
    }
    with mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {element: PARITY_TEST_UNIT}):
        return compose_net_input(request, dictionary, element)
REFERENCE_PICKLE_GLOBALS = {
    ("numpy", "dtype"),
    ("numpy", "ndarray"),
    ("numpy.core.multiarray", "_reconstruct"),
    ("numpy.core.multiarray", "scalar"),
    ("numpy.core.numeric", "_frombuffer"),
}


class ReferenceUnpickler(pickle.Unpickler):
    """Decode the pinned NumPy records without resolving arbitrary globals."""

    def find_class(self, module, name):
        if (module, name) not in REFERENCE_PICKLE_GLOBALS:
            raise pickle.UnpicklingError("Cannot read prediction fixture: serialized type is not approved")
        return super().find_class(module, name)


def load_reference_pickle(content):
    if any(opcode.name in FORBIDDEN_PICKLE_OPCODES for opcode, _, _ in pickletools.genops(content)):
        raise pickle.UnpicklingError("Cannot read prediction fixture: pickle extension and persistent IDs are not approved")
    return ReferenceUnpickler(io.BytesIO(content)).load()


class OneRecord:
    def __init__(self, record):
        self.record = record

    def __len__(self):
        return 1

    def __getitem__(self, index):
        if index != 0:
            raise IndexError(index)
        return self.record


def upstream_net_input(atoms, coordinates, dictionary, selected_token, label_mask=None):
    record = {"atoms": atoms, "coordinates": np.asarray(coordinates)}
    if label_mask is not None:
        record["atoms_target_mask"] = np.asarray(label_mask)
    source = OneRecord(record)
    cropped = CroppingDataset(source, 42, "atoms", "coordinates", max_atoms=128)
    normalized = NormalizeDataset(cropped, "coordinates")

    tokens = TokenizeDataset(KeyDataset(normalized, "atoms"), dictionary, max_seq_len=130)
    token_mask = KeyDataset(normalized, "atoms_target_mask") if label_mask is not None else None
    selection = SelectTokenDataset(tokens, token_mask_dataset=token_mask, selected_token=selected_token)
    tokens = AppendTokenDataset(PrependTokenDataset(tokens, dictionary.bos()), dictionary.eos())
    selection = AppendTokenDataset(
        PrependTokenDataset(selection, dictionary.pad()), dictionary.pad()
    )

    coordinates = ToTorchDataset(KeyDataset(normalized, "coordinates"), "float32")
    distances = PrependAndAppend2DDataset(DistanceDataset(coordinates), 0.0)
    coordinates = AppendTokenDataset(
        PrependTokenDataset(coordinates, 0.0),
        0.0,
    )
    edge_types = EdgeTypeDataset(tokens, len(dictionary))

    return {
        "select_atom": RightPadDataset(selection, dictionary.pad()).collater([selection[0]]),
        "src_tokens": RightPadDataset(tokens, dictionary.pad()).collater([tokens[0]]),
        "src_coord": RightPadDataset2D0(coordinates, 0).collater([coordinates[0]]),
        "src_distance": RightPadDataset2D(distances, 0).collater([distances[0]]),
        "src_edge_type": RightPadDataset2D(edge_types, 0).collater([edge_types[0]]),
    }


def read_receipted_bytes(path, identity):
    """Read one exact receipt-sized file before its bytes reach a decoder.

    LMDB opens a filename rather than a byte stream, so parity tests copy only
    these verified bytes into private container scratch before opening it.
    """
    size = identity.get("size") if isinstance(identity, dict) else None
    digest = identity.get("sha256") if isinstance(identity, dict) else None
    if type(size) is not int or not 0 <= size <= MAX_REFERENCE_FILE_BYTES:
        raise ValueError("Cannot read prediction fixture: receipted file size is outside the 64 MiB test bound")
    if not isinstance(digest, str) or len(digest) != 64:
        raise ValueError("Cannot read prediction fixture: file receipt has no SHA-256 identity")
    with artifacts.regular_file(path, os.O_RDONLY) as stream:
        if os.fstat(stream.fileno()).st_size > size:
            raise ValueError("Cannot read prediction fixture: source exceeds its receipted size")
        content = stream.read(size + 1)
    if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Cannot read prediction fixture: bytes differ from their receipt")
    return content


def read_fixture_receipt(path):
    """Read a small fixture receipt from one regular-file descriptor."""
    with artifacts.regular_file(path, os.O_RDONLY) as stream:
        if os.fstat(stream.fileno()).st_size > MAX_PROVENANCE_BYTES:
            raise ValueError("Cannot read prediction fixture: receipt exceeds the 1 MiB test bound")
        content = stream.read(MAX_PROVENANCE_BYTES + 1)
    if len(content) > MAX_PROVENANCE_BYTES:
        raise ValueError("Cannot read prediction fixture: receipt exceeds the 1 MiB test bound")
    return content


class PredictionModelParityTests(unittest.TestCase):
    def setUp(self):
        self.dictionary = Dictionary()
        for symbol in (
            self.dictionary.bos_word,
            self.dictionary.pad_word,
            self.dictionary.eos_word,
            self.dictionary.unk_word,
        ):
            self.dictionary.add_symbol(symbol, is_special=True)
        for symbol in ("H", "C", "O", "N"):
            self.dictionary.add_symbol(symbol)
        self.mask_index = self.dictionary.add_symbol("[MASK]", is_special=True)
        self.assertEqual(self.mask_index, len(self.dictionary) - 1)

    def test_predict_element_uses_selected_head_and_inverts_scaler_once(self):
        class Model(torch.nn.Module):
            def forward(self, **inputs):
                self.called_in_eval = not self.training
                self.called_without_grad = not torch.is_grad_enabled()
                self.call_options = (inputs["features_only"], inputs["classification_head_name"])
                selected = inputs["src_tokens"][inputs["select_atom"].bool()]
                return (selected.to(torch.float32).reshape(-1, 1), None, None, None, None, None)

        class Scaler:
            calls = 0

            def inverse_transform(self, values):
                self.calls += 1
                self.input = values
                return values.numpy().astype(np.float64) * 2

        request = {
            "coordinate_unit": PARITY_TEST_UNIT,
            "atoms": ["H", "C", "O", "H"],
            "coordinates": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
        }
        dictionary = Dictionary()
        for symbol in (
            dictionary.pad_word,
            dictionary.bos_word,
            dictionary.eos_word,
            dictionary.unk_word,
        ):
            dictionary.add_symbol(symbol, is_special=True)
        for symbol in ("H", "C", "O", "N"):
            dictionary.add_symbol(symbol)
        dictionary.add_symbol("[MASK]", is_special=True)
        self.assertEqual(0, dictionary.pad())
        model = Model()
        scaler = Scaler()
        with mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": PARITY_TEST_UNIT}):
            result = predict_element(request, "H", model, dictionary, scaler)

        hydrogen_token = dictionary.indices["H"]
        self.assertEqual(((0, float(hydrogen_token * 2)), (3, float(hydrogen_token * 2))), result)
        self.assertTrue(model.called_in_eval)
        self.assertTrue(model.called_without_grad)
        self.assertEqual((True, "nmr_head"), model.call_options)
        self.assertEqual(1, scaler.calls)
        self.assertIsInstance(scaler.input, torch.Tensor)
        self.assertEqual(("cpu", torch.float32), (scaler.input.device.type, scaler.input.dtype))

    def test_predict_element_rejects_wrong_head_rows_before_inverse_scaling(self):
        class WrongShapeModel(torch.nn.Module):
            def forward(self, **_inputs):
                return (torch.zeros((1, 2), dtype=torch.float32), None, None, None, None, None)

        scaler = mock.Mock()
        request = {
            "coordinate_unit": PARITY_TEST_UNIT,
            "atoms": ["H"],
            "coordinates": [[0, 0, 0]],
        }
        with mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": PARITY_TEST_UNIT}):
            with self.assertRaisesRegex(
                ValueError,
                r"one scalar row per selected atom \(expected shape \(1, 1\), received \(1, 2\)\)",
            ):
                predict_element(request, "H", WrongShapeModel(), self.dictionary, scaler)
        scaler.inverse_transform.assert_not_called()

    def test_predict_element_rejects_non_float32_output_before_inverse_scaling(self):
        class WrongDtypeModel(torch.nn.Module):
            def forward(self, **_inputs):
                return (torch.zeros((1, 1), dtype=torch.float64), None, None, None, None, None)

        scaler = mock.Mock()
        request = {
            "coordinate_unit": PARITY_TEST_UNIT,
            "atoms": ["H"],
            "coordinates": [[0, 0, 0]],
        }
        with mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": PARITY_TEST_UNIT}):
            with self.assertRaisesRegex(ValueError, "output must be CPU float32"):
                predict_element(request, "H", WrongDtypeModel(), self.dictionary, scaler)
        scaler.inverse_transform.assert_not_called()

    def test_predict_element_rejects_nonfinite_model_and_inverse_scaled_values(self):
        class FixedModel(torch.nn.Module):
            def __init__(self, output):
                super().__init__()
                self.output = output

            def forward(self, **_inputs):
                return (self.output, None, None, None, None, None)

        request = {
            "coordinate_unit": PARITY_TEST_UNIT,
            "atoms": ["H"],
            "coordinates": [[0, 0, 0]],
        }
        with mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": PARITY_TEST_UNIT}):
            model_scaler = mock.Mock()
            with self.assertRaisesRegex(ValueError, "nmr_head output contains non-finite values"):
                predict_element(
                    request,
                    "H",
                    FixedModel(torch.tensor([[float("nan")]], dtype=torch.float32)),
                    self.dictionary,
                    model_scaler,
                )
            model_scaler.inverse_transform.assert_not_called()

            inverse_scaler = mock.Mock()
            inverse_scaler.inverse_transform.return_value = np.array(
                [[float("inf")]], dtype=np.float64
            )
            with self.assertRaisesRegex(ValueError, "inverse-scaled ppm values are not finite"):
                predict_element(
                    request,
                    "H",
                    FixedModel(torch.tensor([[0.5]], dtype=torch.float32)),
                    self.dictionary,
                    inverse_scaler,
                )
            inverse_scaler.inverse_transform.assert_called_once()

    def test_direct_predict_element_call_rejects_an_unqualified_head_before_forward(self):
        model = mock.Mock()
        scaler = mock.Mock()
        request = {
            "coordinate_unit": PARITY_TEST_UNIT,
            "atoms": ["H"],
            "coordinates": [[0, 0, 0]],
        }
        with mock.patch.dict(predict.QUALIFIED_MODEL_COORDINATE_UNITS, {"H": None}):
            with self.assertRaisesRegex(RuntimeError, "has not been qualified"):
                predict_element(request, "H", model, self.dictionary, scaler)
        model.eval.assert_not_called()
        model.assert_not_called()
        scaler.inverse_transform.assert_not_called()

    def test_receipted_fixture_bytes_reject_initial_oversize_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.bin"
            path.write_bytes(b"larger than receipt")
            real_regular_file = artifacts.regular_file
            read_sizes = []

            class Reader:
                def __init__(self, stream):
                    self.stream = stream

                def fileno(self):
                    return self.stream.fileno()

                def read(self, size=-1):
                    read_sizes.append(size)
                    return self.stream.read(size)

            @contextmanager
            def observe_reads(source, flags):
                with real_regular_file(source, flags) as stream:
                    yield Reader(stream)

            identity = {"size": 1, "sha256": hashlib.sha256(b"x").hexdigest()}
            with mock.patch.object(artifacts, "regular_file", side_effect=observe_reads):
                with self.assertRaisesRegex(ValueError, "source exceeds its receipted size"):
                    read_receipted_bytes(path, identity)
            self.assertEqual(read_sizes, [])

    def test_receipted_fixture_bytes_cap_growth_after_size_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.bin"
            original = b"fixture"
            path.write_bytes(original)
            real_regular_file = artifacts.regular_file
            read_sizes = []
            grew = False

            class Reader:
                def __init__(self, stream):
                    self.stream = stream

                def fileno(self):
                    return self.stream.fileno()

                def read(self, size=-1):
                    nonlocal grew
                    read_sizes.append(size)
                    if not grew:
                        with path.open("ab") as writer:
                            writer.write(b"x")
                        grew = True
                    return self.stream.read(size)

            @contextmanager
            def grow_after_fstat(source, flags):
                with real_regular_file(source, flags) as stream:
                    yield Reader(stream)

            identity = {"size": len(original), "sha256": hashlib.sha256(original).hexdigest()}
            with mock.patch.object(artifacts, "regular_file", side_effect=grow_after_fstat):
                with self.assertRaisesRegex(ValueError, "bytes differ from their receipt"):
                    read_receipted_bytes(path, identity)
            self.assertEqual(read_sizes, [len(original) + 1])
            self.assertEqual(path.stat().st_size, len(original) + 1)

    def test_receipted_fixture_bytes_keep_the_opened_file_after_path_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.bin"
            replacement = Path(directory) / "replacement.bin"
            original = b"receipted bytes"
            path.write_bytes(original)
            real_regular_file = artifacts.regular_file

            @contextmanager
            def replace_after_open(source, flags):
                with real_regular_file(source, flags) as stream:
                    replacement.write_bytes(b"different bytes")
                    replacement.replace(path)
                    yield stream

            identity = {"size": len(original), "sha256": hashlib.sha256(original).hexdigest()}
            with mock.patch.object(artifacts, "regular_file", side_effect=replace_after_open):
                self.assertEqual(read_receipted_bytes(path, identity), original)
            self.assertEqual(path.read_bytes(), b"different bytes")

    def test_reference_pickle_does_not_resolve_arbitrary_python_globals(self):
        import os

        content = pickle.dumps(os.system)
        with self.assertRaisesRegex(pickle.UnpicklingError, "serialized type is not approved"):
            load_reference_pickle(content)

    def test_reference_pickle_rejects_extension_and_persistent_id_opcodes(self):
        fixtures = {
            "EXT1": b"\x80\x02\x82\x01.",
            "EXT2": b"\x80\x02\x83\x01\x00.",
            "EXT4": b"\x80\x02\x84\x01\x00\x00\x00.",
            "PERSID": b"Punsafe\n.",
            "BINPERSID": b"\x80\x02U\x03keyQ.",
        }
        for opcode, content in fixtures.items():
            with self.subTest(opcode=opcode):
                with self.assertRaisesRegex(pickle.UnpicklingError, "extension and persistent IDs"):
                    load_reference_pickle(content)


    def test_one_and_maximum_atoms_match_upstream_for_h_and_c(self):
        samples = (
            (["H"], [[7.0, -3.0, 2.0]]),
            (
                ["H", "C", "O", "N"] * 32,
                [
                    [index * 0.123456789, (index * 7) % 19 + 0.1, (index * 13) % 23 + 0.03]
                    for index in range(128)
                ],
            ),
        )
        for atoms, coordinates in samples:
            for element in ("H", "C"):
                with self.subTest(atom_count=len(atoms), element=element):
                    selected = self.dictionary.vec_index([element])
                    composed = compose_for_tensor_parity(atoms, coordinates, self.dictionary, element)
                    actual = composed.model_inputs
                    expected = upstream_net_input(atoms, coordinates, self.dictionary, selected)
                    self.assertEqual(
                        composed.selected_atom_indices,
                        tuple(index for index, atom in enumerate(atoms) if atom == element),
                    )
                    self.assertEqual(set(actual), set(expected))
                    mask_indices = torch.where(
                        actual["select_atom"][0, 1 : len(atoms) + 1] == 1
                    )[0].tolist()
                    self.assertEqual(mask_indices, list(composed.selected_atom_indices))
                    for name in expected:
                        self.assertEqual(actual[name].dtype, expected[name].dtype, name)
                        self.assertEqual(actual[name].shape, expected[name].shape, name)
                        self.assertEqual(actual[name].device.type, "cpu", name)
                        torch.testing.assert_close(actual[name], expected[name], rtol=0, atol=0, msg=name)

                    logical_length = len(atoms) + 2
                    padded_length = 8 if len(atoms) == 1 else 136
                    self.assertEqual(actual["src_tokens"].shape, (1, padded_length))
                    self.assertEqual(actual["src_coord"].shape, (1, padded_length, 3))
                    self.assertEqual(actual["src_distance"].shape, (1, padded_length, padded_length))
                    self.assertEqual(actual["src_edge_type"].shape, (1, padded_length, padded_length))
                    self.assertTrue(torch.equal(actual["src_coord"][0, 0], torch.zeros(3)))
                    self.assertTrue(torch.equal(actual["src_coord"][0, len(atoms) + 1], torch.zeros(3)))
                    self.assertTrue(torch.equal(actual["src_coord"][0, logical_length:], torch.zeros(padded_length - logical_length, 3)))
                    self.assertTrue(torch.equal(actual["src_distance"][0, 0], torch.zeros(padded_length)))
                    self.assertTrue(torch.equal(actual["src_distance"][0, len(atoms) + 1], torch.zeros(padded_length)))

    def test_absent_element_has_no_selected_indices_or_mask_rows(self):
        atoms = ["O", "N"]
        coordinates = [[0.0, 0.0, 0.0], [1.0, 2.0, 3.0]]
        for element in ("H", "C"):
            with self.subTest(element=element):
                composed = compose_for_tensor_parity(atoms, coordinates, self.dictionary, element)
                expected = upstream_net_input(
                    atoms,
                    coordinates,
                    self.dictionary,
                    self.dictionary.vec_index([element]),
                )
                self.assertEqual(composed.selected_atom_indices, ())
                self.assertTrue(
                    torch.equal(
                        composed.model_inputs["select_atom"],
                        expected["select_atom"],
                    )
                )
                real_atom_mask = composed.model_inputs["select_atom"][0, 1 : len(atoms) + 1]
                self.assertEqual(real_atom_mask.sum().item(), 0)

    def test_oversize_input_rejects_before_scientific_imports_or_pairwise_allocation(self):
        from unittest.mock import patch

        with (
            patch("scipy.spatial.distance_matrix") as pairwise,
            patch.dict("sys.modules", {"numpy": None, "torch": None, "scipy": None}),
        ):
            with self.assertRaisesRegex(ValueError, "1 through 128 symbols"):
                compose_for_tensor_parity(
                    ["H"] * 129,
                    [[float(index), 0.0, 0.0] for index in range(129)],
                    self.dictionary,
                    "H",
                )
        pairwise.assert_not_called()

    def test_coordinate_rows_must_match_atom_count_and_three_axes(self):
        with self.assertRaisesRegex(ValueError, "one three-value row per atom"):
            compose_for_tensor_parity(["H", "C"], [[0.0, 0.0, 0.0]], self.dictionary, "H")

    def test_coordinate_values_must_be_scalar_numbers(self):
        coordinates = [[[0.0, 0.0, 0.0], 0.0, 0.0]]
        with self.assertRaisesRegex(ValueError, "one three-value row per atom"):
            compose_for_tensor_parity(["H"], coordinates, self.dictionary, "H")

    def test_unsupported_atom_diagnostic_does_not_echo_input(self):
        private_input = "private-note-phrase"
        with self.assertRaisesRegex(ValueError, "atom at input index 0") as error:
            compose_for_tensor_parity([private_input], [[0.0, 0.0, 0.0]], self.dictionary, "H")
        self.assertNotIn(private_input, str(error.exception))

    def test_unrepresentable_integer_has_a_compose_diagnostic(self):
        with self.assertRaisesRegex(ValueError, "exceed the finite float32 range"):
            compose_for_tensor_parity(["H"], [[10**500, 0, 0]], self.dictionary, "H")


@unittest.skipUnless(Path("/prepared/provenance.json").is_file(), "requires the contained 001 prepared reference")
class PreparedReferenceTensorParityTests(unittest.TestCase):
    def test_four_receipted_records_match_upstream_geometry_without_label_inputs(self):
        provenance_path = Path("/prepared/provenance.json")
        provenance_bytes = read_fixture_receipt(provenance_path)
        provenance = json.loads(provenance_bytes)
        reference_bytes = read_fixture_receipt(Path("/input/reference-cpu-float32.json"))
        self.assertEqual(hashlib.sha256(reference_bytes).hexdigest(), EXPECTED_REFERENCE_PACKET_SHA256)
        reference_packet = json.loads(reference_bytes)
        expected_receipt = reference_packet["evidence_identities"]["prepared_receipt"]
        self.assertEqual(len(provenance_bytes), expected_receipt["size"])
        self.assertEqual(hashlib.sha256(provenance_bytes).hexdigest(), expected_receipt["sha256"])
        self.assertEqual(provenance["record_id"], reference_packet["release"]["record_id"])
        self.assertEqual(provenance["selection"], {"count_per_element": 2, "max_atoms": 128, "max_tokens": 130})
        self.assertEqual(provenance["selection"], reference_packet["selection"])
        self.assertEqual(set(provenance["elements"]), {"H", "C"})
        self.assertEqual(provenance["dictionary"], reference_packet["dictionary"])
        expected_symbols = provenance["dictionary"]["symbols"]
        mask_token = provenance["dictionary"]["task_special_token"]
        self.assertEqual(mask_token, {"symbol": "[MASK]", "index": len(expected_symbols) - 1})
        self.assertEqual(expected_symbols[-1], "[MASK]")

        total_records = 0
        with tempfile.TemporaryDirectory(prefix="nmrnet-tensor-parity-") as temporary:
            scratch = Path(temporary)
            for fixture_element in ("H", "C"):
                element_root = Path("/prepared") / fixture_element
                identities = provenance["elements"][fixture_element]["files"]
                fixture_root = scratch / fixture_element
                fixture_root.mkdir()
                database_bytes = read_receipted_bytes(element_root / "valid.lmdb", identities["valid.lmdb"])
                key_bytes = read_receipted_bytes(element_root / "valid_dbid.pkl", identities["valid_dbid.pkl"])
                dictionary_bytes = read_receipted_bytes(element_root / "dict.txt", identities["dict.txt"])

                database_path = fixture_root / "valid.lmdb"
                database_path.write_bytes(database_bytes)
                keys = load_reference_pickle(key_bytes)
                record_receipts = provenance["elements"][fixture_element]["records"]
                expected_records = reference_packet["elements"][fixture_element]["records"]
                self.assertEqual(len(keys), 2)
                self.assertEqual(
                    [
                        {field: item[field] for field in ("key_hex", "raw_sha256", "atom_count", "selected_atom_indices")}
                        for item in record_receipts
                    ],
                    [
                        {field: item[field] for field in ("key_hex", "raw_sha256", "atom_count", "selected_atom_indices")}
                        for item in expected_records
                    ],
                )
                self.assertEqual([key.hex() for key in keys], [item["key_hex"] for item in record_receipts])

                dictionary = Dictionary.load(io.StringIO(dictionary_bytes.decode("utf-8")))
                self.assertEqual(dictionary.symbols, provenance["dictionary"]["symbols_before_task"])
                self.assertEqual(
                    dictionary.add_symbol("[MASK]", is_special=True),
                    provenance["dictionary"]["task_special_token"]["index"],
                )
                self.assertEqual(dictionary.symbols, expected_symbols)

                import lmdb

                with lmdb.open(
                    str(database_path),
                    subdir=False,
                    readonly=True,
                    lock=False,
                    readahead=False,
                    meminit=False,
                ) as environment, environment.begin() as transaction:
                    self.assertEqual(transaction.stat()["entries"], len(keys))
                    for key, record_receipt in zip(keys, record_receipts):
                        packed_record = transaction.get(key)
                        self.assertIsNotNone(packed_record)
                        self.assertEqual(hashlib.sha256(packed_record).hexdigest(), record_receipt["raw_sha256"])
                        record = load_reference_pickle(packed_record)
                        atoms = list(record["atoms"])
                        label_mask = list(record["atoms_target_mask"])
                        self.assertEqual(len(atoms), record_receipt["atom_count"])
                        self.assertEqual(len(label_mask), len(atoms))
                        labelled_fixture_indices = [
                            index for index, (atom, selected) in enumerate(zip(atoms, label_mask))
                            if atom == fixture_element and selected == 1
                        ]
                        self.assertEqual(labelled_fixture_indices, record_receipt["selected_atom_indices"])

                        coordinates = record["coordinates"]
                        request_coordinates = np.asarray(coordinates).tolist()
                        for element in ("H", "C"):
                            with self.subTest(fixture=fixture_element, element=element):
                                composed = compose_for_tensor_parity(atoms, request_coordinates, dictionary, element)
                                # The oracle keeps the released label mask; production receives only atoms and coordinates.
                                expected = upstream_net_input(
                                    atoms,
                                    coordinates,
                                    dictionary,
                                    dictionary.vec_index([element]),
                                    label_mask=label_mask,
                                )
                                self.assertEqual(
                                    composed.selected_atom_indices,
                                    tuple(index for index, atom in enumerate(atoms) if atom == element),
                                )
                                expected_label_indices = tuple(
                                    index for index, (atom, selected) in enumerate(zip(atoms, label_mask))
                                    if atom == element and selected == 1
                                )
                                oracle_mask = torch.where(expected["select_atom"][0, 1 : len(atoms) + 1] == 1)[0].tolist()
                                self.assertEqual(tuple(oracle_mask), expected_label_indices)
                                actual_mask = composed.model_inputs["select_atom"][0, 1 : len(atoms) + 1]
                                self.assertEqual(
                                    torch.where(actual_mask == 1)[0].tolist(),
                                    list(composed.selected_atom_indices),
                                )
                                for name in ("src_tokens", "src_coord", "src_distance", "src_edge_type"):
                                    actual = composed.model_inputs[name]
                                    reference_tensor = expected[name]
                                    self.assertEqual(actual.dtype, reference_tensor.dtype, name)
                                    self.assertEqual(actual.shape, reference_tensor.shape, name)
                                    self.assertEqual(actual.device.type, "cpu", name)
                                    torch.testing.assert_close(actual, reference_tensor, rtol=0, atol=0, msg=name)
                        total_records += 1
        self.assertEqual(total_records, 4)




class VerifiedModelArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = {"name": "weights.zip", "size": 100, "md5": "0" * 32}
        self.provenance = {
            "archive": self.archive,
            "archive_sha256": "1" * 64,
            "members": {},
        }

    def add_member(self, name, content, *, max_bytes=None):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.provenance["members"][name] = {
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        (self.root / "provenance.json").write_text(json.dumps(self.provenance))
        selection = {
            "members": [name],
            "max_bytes": len(content) if max_bytes is None else max_bytes,
        }
        return path, selection

    def test_verified_member_snapshot_reports_the_bytes_it_returns(self):
        content = b"model release member"
        path, selection = self.add_member("model/checkpoint.pt", content)
        snapshot, identity = baseline_prepare.verified_member_bytes(
            self.root, self.archive, selection
        )
        self.assertEqual(snapshot, content)
        self.assertEqual(
            identity,
            {
                "archive": self.archive,
                "archive_sha256": "1" * 64,
                "member": "model/checkpoint.pt",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
        )
        verified_path, path_identity = baseline_prepare.verified_member(
            self.root, self.archive, selection
        )
        self.assertEqual(verified_path, path)
        self.assertEqual(path_identity["sha256"], identity["sha256"])
        self.assertEqual(path.read_bytes(), content)

    def test_verified_member_snapshot_rejects_receipt_size_or_hash_mismatch(self):
        content = b"model release member"
        for field, wrong_value in (
            ("size", len(content) + 1),
            ("sha256", "2" * 64),
        ):
            with self.subTest(field=field):
                _path, selection = self.add_member("model/checkpoint.pt", content)
                self.provenance["members"]["model/checkpoint.pt"][field] = wrong_value
                (self.root / "provenance.json").write_text(json.dumps(self.provenance))
                with self.assertRaisesRegex(ValueError, f"{field} differs from extraction provenance"):
                    baseline_prepare.verified_member_bytes(self.root, self.archive, selection)

    def test_verified_member_snapshot_rejects_initial_oversize_before_reading(self):
        content = b"larger than the admitted member bound"
        path, selection = self.add_member("model/checkpoint.pt", content, max_bytes=4)
        real_regular_file = artifacts.regular_file
        read_sizes = []

        class Reader:
            def __init__(self, stream):
                self.stream = stream

            def fileno(self):
                return self.stream.fileno()

            def read(self, size=-1):
                read_sizes.append(size)
                return self.stream.read(size)

        @contextmanager
        def observe_reads(source, flags):
            with real_regular_file(source, flags) as stream:
                yield Reader(stream)

        with mock.patch.object(artifacts, "regular_file", side_effect=observe_reads):
            with self.assertRaisesRegex(ValueError, "size exceeds selection max_bytes=4"):
                baseline_prepare.verified_member_bytes(self.root, self.archive, selection)
        self.assertEqual(read_sizes, [])
        self.assertEqual(path.read_bytes(), content)

    def test_verified_member_snapshot_caps_growth_during_read(self):
        content = b"bounded"
        path, selection = self.add_member("model/checkpoint.pt", content)
        real_regular_file = artifacts.regular_file
        read_sizes = []
        grew = False

        class Reader:
            def __init__(self, stream):
                self.stream = stream

            def fileno(self):
                return self.stream.fileno()

            def read(self, size=-1):
                nonlocal grew
                read_sizes.append(size)
                if not grew:
                    with path.open("ab") as writer:
                        writer.write(b"x")
                    grew = True
                return self.stream.read(size)

        @contextmanager
        def grow_after_size_check(source, flags):
            with real_regular_file(source, flags) as stream:
                yield Reader(stream)

        with mock.patch.object(artifacts, "regular_file", side_effect=grow_after_size_check):
            with self.assertRaisesRegex(ValueError, "source exceeds selection max_bytes"):
                baseline_prepare.verified_member_bytes(self.root, self.archive, selection)
        self.assertEqual(read_sizes, [len(content) + 1])
        self.assertEqual(path.stat().st_size, len(content) + 1)

    def test_checkpoint_scaler_and_dictionary_load_only_verified_byte_snapshots(self):
        from sklearn.preprocessing import StandardScaler

        checkpoint_buffer = io.BytesIO()
        torch.save({"model": {"weight": torch.tensor([2.5])}}, checkpoint_buffer)
        scaler_buffer = io.BytesIO()
        import joblib

        joblib.dump(StandardScaler().fit(np.array([[1.0], [3.0]])), scaler_buffer)
        members = (
            ("model/checkpoint.pt", checkpoint_buffer.getvalue(), load_checkpoint_from_bytes),
            ("model/target_scaler.ss", scaler_buffer.getvalue(), load_scaler_from_bytes),
            ("model/dict.txt", b"C 1\nH 2\n", load_dictionary_from_bytes),
        )

        for name, content, loader in members:
            with self.subTest(member=name):
                path, selection = self.add_member(name, content)
                snapshot, identity = baseline_prepare.verified_member_bytes(
                    self.root, self.archive, selection
                )
                self.assertEqual(identity["sha256"], hashlib.sha256(snapshot).hexdigest())
                self.assertEqual(identity["size"], len(snapshot))

                path.write_bytes(b"changed in place after verification")
                loaded = loader(snapshot)
                if name.endswith("checkpoint.pt"):
                    self.assertEqual(loaded["model"]["weight"].tolist(), [2.5])
                elif name.endswith("target_scaler.ss"):
                    np.testing.assert_array_equal(loaded.mean_, np.array([2.0]))
                else:
                    self.assertEqual(loaded.symbols, ["C", "H"])

                replacement = path.with_name("replacement")
                replacement.write_bytes(b"replacement path contents")
                replacement.replace(path)
                self.assertIsNotNone(loader(snapshot))
                with self.assertRaises(TypeError):
                    loader(path)
if __name__ == "__main__":
    unittest.main()
