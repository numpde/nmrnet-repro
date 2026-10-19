"""Qualify the actual SMILES producer in the pinned preparation runtime.

These fixtures establish generated coordinates, atom mapping and honest shortfall
handling. They do not qualify a worker channel, interpreter or prediction accuracy.
"""

from tools.container_guard import require_container

require_container()

import hashlib
import json
import math
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from rdkit import Chem
from rdkit.Chem import rdDistGeom

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.smiles import ConformerShortfall, generate_conformers

SETTINGS = {"seed": 20261019, "max_iterations": 1280, "timeout_seconds": 1}


class SmilesNormativeTests(unittest.TestCase):
    def setUp(self):
        self.corpus = json.loads((Path(__file__).parent / "fixtures/smiles.json").read_bytes())

    def test_all_authored_cases_preserve_source_hydrogens_and_generated_parent_maps(self):
        for case in self.corpus["normative"]:
            with self.subTest(case=case["name"]):
                source = case["smiles"].encode()
                batch = generate_conformers(source, case["requested_conformers"], **SETTINGS)
                metadata = json.loads(batch.provenance)
                self.assertEqual(metadata["source_sha256"], hashlib.sha256(source).hexdigest())
                self.assertEqual(metadata["requested_count"], case["requested_conformers"])
                self.assertEqual(metadata["runtime"]["rdkit"], "2026.09.1")
                self.assertEqual([atom["element"] for atom in metadata["source_atoms"]], case["source_atoms"])
                self.assertEqual(len(batch.conformers), case["requested_conformers"])
                for conformer in batch.conformers:
                    request = json.loads(conformer.request_bytes)
                    self.assertEqual(request["coordinate_unit"], "angstrom")
                    self.assertEqual(Counter(request["atoms"]), Counter(case["generated_atom_counts"]))
                    self.assertEqual(request["atoms"][: len(case["source_atoms"])], case["source_atoms"])
                    self.assertEqual(len(request["atoms"]), len(request["coordinates"]))
                    self.assertTrue(all(math.isfinite(value) for row in request["coordinates"] for value in row))
                    added = metadata["added_hydrogen_parents"]
                    self.assertEqual([row[0] for row in added], list(range(len(case["source_atoms"]), len(request["atoms"]))))
                    for index, parent in added:
                        self.assertEqual(request["atoms"][index], "H")
                        self.assertLess(parent, len(case["source_atoms"]))
                        self.assertNotEqual(request["atoms"][parent], "H")

    def test_effective_settings_are_captured_at_the_actual_embedding_call(self):
        original = rdDistGeom.EmbedMultipleConfs
        observed = []

        def embed(molecule, *, numConfs, params):
            observed.append((numConfs, json.loads(rdDistGeom.EmbedParametersToJSON(params))))
            return original(molecule, numConfs=numConfs, params=params)

        with patch.object(rdDistGeom, "EmbedMultipleConfs", side_effect=embed) as call:
            batch = generate_conformers(b"CCCC", 3, **SETTINGS)
        call.assert_called_once()
        metadata = json.loads(batch.provenance)
        self.assertEqual(observed, [(3, metadata["embedding"]["parameters"])])
        self.assertEqual(metadata["embedding"]["preset"], "ETKDGv3")
        self.assertFalse(metadata["parser"]["removeHs"])
        self.assertEqual(metadata["hydrogen_expansion"], {"explicitOnly": False, "addCoords": False})

    def test_source_atom_map_labels_and_opposite_stereochemical_tags_survive(self):
        mapped = generate_conformers(b"[CH3:42]Cl", 1, **SETTINGS)
        self.assertEqual(json.loads(mapped.provenance)["source_atoms"][0]["atom_map_number"], 42)
        tags = []
        for source in (b"F[C@H](Cl)Br", b"F[C@@H](Cl)Br"):
            batch = generate_conformers(source, 1, **SETTINGS)
            specified = json.loads(batch.provenance)["source_atoms"][1]
            assigned = json.loads(batch.conformers[0].stereochemistry)["atoms"]
            self.assertEqual(specified["cip_code"], next(atom["cip_code"] for atom in assigned if atom["atom_index"] == 1))
            tags.append(specified["chiral_tag"])
        self.assertNotEqual(tags[0], tags[1])

    def test_isotopes_source_bond_stereo_and_generated_stereo_stay_distinct(self):
        isotopic = generate_conformers(b"[2H]C", 1, **SETTINGS)
        metadata = json.loads(isotopic.provenance)
        self.assertEqual(metadata["source_atoms"][0]["isotope"], 2)
        self.assertIn("not separate prediction features", metadata["model_projection"])
        generated = generate_conformers(b"FC(Cl)Br", 1, **SETTINGS)
        self.assertEqual(json.loads(generated.provenance)["source_atoms"][1]["chiral_tag"], "CHI_UNSPECIFIED")
        stereo = json.loads(generated.conformers[0].stereochemistry)
        self.assertEqual(stereo["origin"], "generated_coordinates")
        self.assertEqual(stereo["atoms"][0]["atom_index"], 1)
        self.assertIn(stereo["atoms"][0]["cip_code"], ("R", "S"))
        bond_stereo = []
        for source in (b"F/C=C/F", b"F/C=C\\F"):
            batch = generate_conformers(source, 1, **SETTINGS)
            specified = [
                bond["stereo"] for bond in json.loads(batch.provenance)["source_bonds"] if bond["stereo"] != "STEREONONE"
            ]
            assigned = [bond["stereo"] for bond in json.loads(batch.conformers[0].stereochemistry)["bonds"]]
            self.assertEqual(assigned, specified)
            bond_stereo.extend(specified)
        self.assertEqual(set(bond_stereo), {"STEREOE", "STEREOZ"})


class SmilesAdversarialTests(unittest.TestCase):
    def test_invalid_count_is_rejected_before_native_parsing(self):
        for count in (None, True, 1.5, "2", 0, -1, 129):
            with self.subTest(count=count), patch.object(Chem, "MolFromSmiles") as parse:
                with self.assertRaises(InputRejected):
                    generate_conformers(b"CCl", count, **SETTINGS)
                parse.assert_not_called()

    def test_invalid_smiles_and_plain_smiles_extensions_never_embed(self):
        for source in (b"", b"C(", b"CCl arbitrary-name", b"CCl |atomProp:0.foo.bar|", b"C\x00N", b"CCl\nCCl"):
            with self.subTest(source=source), patch.object(rdDistGeom, "EmbedMultipleConfs") as embed:
                with self.assertRaises(InputRejected) as caught:
                    generate_conformers(source, 1, **SETTINGS)
                if source:
                    self.assertNotIn(source.decode(), str(caught.exception))
                embed.assert_not_called()

    def test_invalid_host_budgets_fail_before_native_parsing(self):
        for name, value in (
            ("seed", -1),
            ("seed", True),
            ("max_iterations", 0),
            ("timeout_seconds", 0),
            ("timeout_seconds", 0.5),
        ):
            with self.subTest(name=name, value=value), patch.object(Chem, "MolFromSmiles") as parse:
                with self.assertRaises(ValueError):
                    generate_conformers(b"CCl", 1, **(SETTINGS | {name: value}))
                parse.assert_not_called()

    def test_expanded_atom_limit_rejects_before_embedding(self):
        with patch.object(rdDistGeom, "EmbedMultipleConfs") as embed:
            with self.assertRaisesRegex(InputRejected, "128"):
                generate_conformers(b"C" * 43, 1, **SETTINGS)
            embed.assert_not_called()

    def test_shortfall_retains_actual_noncontiguous_ids_and_coordinates_without_retry(self):
        original = rdDistGeom.EmbedMultipleConfs

        def embed(molecule, *, numConfs, params):
            original(molecule, numConfs=numConfs, params=params)
            return [0, 2]

        with (
            patch.object(rdDistGeom, "EmbedMultipleConfs", side_effect=embed) as call,
            self.assertRaises(ConformerShortfall) as caught,
        ):
            generate_conformers(b"CCCC", 3, **SETTINGS)
        call.assert_called_once()
        self.assertEqual([item.conformer_id for item in caught.exception.partial.conformers], [0, 2])
        self.assertEqual(json.loads(caught.exception.partial.provenance)["requested_count"], 3)
        self.assertTrue(all(json.loads(item.request_bytes)["coordinates"] for item in caught.exception.partial.conformers))

    def test_zero_conformers_is_whole_failure_and_native_exception_keeps_its_type(self):
        with (
            patch.object(rdDistGeom, "EmbedMultipleConfs", return_value=[]) as embed,
            self.assertRaises(ConformerShortfall) as caught,
        ):
            generate_conformers(b"CCCC", 3, **SETTINGS)
        embed.assert_called_once()
        self.assertEqual(caught.exception.partial.conformers, ())
        failure = RuntimeError("native failure canary")
        with (
            patch.object(rdDistGeom, "EmbedMultipleConfs", side_effect=failure),
            self.assertRaises(RuntimeError) as caught,
        ):
            generate_conformers(b"CCCC", 3, **SETTINGS)
        self.assertIs(caught.exception, failure)
