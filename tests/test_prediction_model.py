"""Compare label-free tensor composition with the pinned upstream datasets."""

import unittest

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

from nmrnet_inference.model import compose_net_input
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


class OneRecord:
    def __init__(self, record):
        self.record = record

    def __len__(self):
        return 1

    def __getitem__(self, index):
        if index != 0:
            raise IndexError(index)
        return self.record


def upstream_net_input(atoms, coordinates, dictionary, selected_token):
    source = OneRecord({"atoms": atoms, "coordinates": np.asarray(coordinates)})
    cropped = CroppingDataset(source, 42, "atoms", "coordinates", max_atoms=128)
    normalized = NormalizeDataset(cropped, "coordinates")

    tokens = TokenizeDataset(KeyDataset(normalized, "atoms"), dictionary, max_seq_len=130)
    selection = SelectTokenDataset(tokens, selected_token=selected_token)
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
                    composed = compose_net_input(atoms, coordinates, self.dictionary, element)
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
                composed = compose_net_input(atoms, coordinates, self.dictionary, element)
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
                compose_net_input(
                    ["H"] * 129,
                    [[float(index), 0.0, 0.0] for index in range(129)],
                    self.dictionary,
                    "H",
                )
        pairwise.assert_not_called()

    def test_coordinate_rows_must_match_atom_count_and_three_axes(self):
        with self.assertRaisesRegex(ValueError, "one three-value row per atom"):
            compose_net_input(["H", "C"], [[0.0, 0.0, 0.0]], self.dictionary, "H")

    def test_coordinate_values_must_be_scalar_numbers(self):
        coordinates = [[[0.0, 0.0, 0.0], 0.0, 0.0]]
        with self.assertRaisesRegex(ValueError, "one three-value row per atom"):
            compose_net_input(["H"], coordinates, self.dictionary, "H")

    def test_unsupported_atom_diagnostic_does_not_echo_input(self):
        private_input = "private-note-phrase"
        with self.assertRaisesRegex(ValueError, "atom at input index 0") as error:
            compose_net_input([private_input], [[0.0, 0.0, 0.0]], self.dictionary, "H")
        self.assertNotIn(private_input, str(error.exception))

    def test_unrepresentable_integer_has_a_compose_diagnostic(self):
        with self.assertRaisesRegex(ValueError, "exceed the finite float32 range"):
            compose_net_input(["H"], [[10**500, 0, 0]], self.dictionary, "H")


if __name__ == "__main__":
    unittest.main()
