"""Untrusted replies cannot invent source identity or executable collections."""

from tools.container_guard import require_container

require_container()

import copy
import hashlib
import json
import unittest
from dataclasses import replace

from tests.unit.smiles_fakes import SETTINGS, SOURCE, collection

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.smiles_protocol import (
    ConformerOutputLimit,
    conformer_reply,
    prepared_conformers,
    read_smiles_request,
    smiles_request,
)
from nmrnet_provider.worker_client import WorkerResultRejected
from nmrnet_provider.worker_protocol import MAX_FRAME_BYTES, WorkerProtocolError, frame


class SmilesProtocolAdversarialTests(unittest.TestCase):
    def test_generated_executable_coordinates_cannot_declare_source_units(self):
        for unit in ("nm", "bohr"):
            reply = self.reply()
            request = json.loads(reply["conformers"][0]["request_utf8"])
            request["coordinate_unit"] = unit
            reply["conformers"][0]["request_utf8"] = json.dumps(request)
            with self.subTest(unit=unit):
                self.rejected(reply)

    def reply(self):
        return conformer_reply(collection(), allocation="fixture")

    def rejected(self, reply):
        with self.assertRaises(WorkerResultRejected) as caught:
            prepared_conformers(reply, SOURCE, 2, **SETTINGS)
        self.assertEqual(json.loads(caught.exception.diagnostic), reply)
        self.assertNotIn("PRIVATE_NATIVE_DIAGNOSTIC", str(caught.exception))
        self.assertNotIn("PRIVATE_NATIVE_DIAGNOSTIC", repr(caught.exception))

    def test_source_bounds_and_invalid_counts_reject_before_dispatch(self):
        for source, count in ((b"C", True), (b"C", 129), (b"C\nC", 1), (b"C" * (1024 * 1024 + 1), 1)):
            with self.subTest(count=count), self.assertRaises(InputRejected):
                smiles_request(source, count)
        request = smiles_request(SOURCE, 2)
        for change in ({"seed": 1}, {"operation": "read_geometry"}, {"smiles_utf8": 42}):
            with self.subTest(change=change), self.assertRaises(WorkerProtocolError):
                read_smiles_request(request | change)

    def test_source_count_and_settings_must_match_the_controller(self):
        for field, value in (("source_sha256", "0" * 64), ("requested_count", 3), ("requested_count", True)):
            reply = self.reply()
            metadata = json.loads(reply["provenance_utf8"])
            metadata[field] = value
            reply["provenance_utf8"] = json.dumps(metadata)
            self.rejected(reply)
        reply = self.reply()
        metadata = json.loads(reply["provenance_utf8"])
        metadata["embedding"]["requested_settings"]["randomSeed"] += 1
        reply["provenance_utf8"] = json.dumps(metadata)
        self.rejected(reply)
        reply = self.reply()
        metadata = json.loads(reply["provenance_utf8"])
        metadata["embedding"]["parameters"]["randomSeed"] = "18"
        reply["provenance_utf8"] = json.dumps(metadata)
        self.rejected(reply)

    def test_wrong_counts_duplicate_boolean_ids_and_nonfinite_coordinates_reject(self):
        variants = []
        for kind in ("unexpected", "conformer_shortfall"):
            variants.append(self.reply() | {"kind": kind})
        reply = self.reply()
        variants.append(reply | {"conformers": reply["conformers"][:1]})
        for identifier in (2, True, -1):
            reply = self.reply()
            reply["conformers"][1]["conformer_id"] = identifier
            variants.append(reply)
        reply = self.reply()
        reply["conformers"][1]["request_utf8"] = '{"coordinate_unit":"angstrom","atoms":["C"],"coordinates":[[NaN,0,0]]}'
        variants.append(reply)
        for reply in variants:
            with self.subTest(reply_kind=reply["kind"]):
                self.rejected(reply)

    def test_false_hydrogen_mapping_and_out_of_range_stereo_references_reject(self):
        for parents in ([[1, 0], [2, 0], [3, 0], [4, 4]], [[2, 0], [1, 0], [3, 0], [4, 0]]):
            reply = self.reply()
            metadata = json.loads(reply["provenance_utf8"])
            metadata["added_hydrogen_parents"] = parents
            reply["provenance_utf8"] = json.dumps(metadata)
            self.rejected(reply)
        reply = self.reply()
        stereo = json.loads(reply["conformers"][0]["stereochemistry_utf8"])
        stereo["atoms"] = [{"atom_index": 5, "chiral_tag": "CHI_TETRAHEDRAL_CW", "cip_code": "R"}]
        reply["conformers"][0]["stereochemistry_utf8"] = json.dumps(stereo)
        self.rejected(reply)

    def test_malformed_nested_metadata_keeps_the_original_private_reply(self):
        for change in ({"provenance_utf8": "[]"}, {"provenance_utf8": '{"x":1,"x":2}'}, {"diagnostic": []}):
            self.rejected(self.reply() | change)
        reply = self.reply()
        altered = copy.deepcopy(reply)
        altered["conformers"][0]["stereochemistry_utf8"] = "not JSON"
        self.rejected(altered)

    def test_missing_provenance_and_false_source_graph_references_reject(self):
        for field in ("source_bonds", "runtime", "parser", "hydrogen_expansion", "model_projection"):
            reply = self.reply()
            metadata = json.loads(reply["provenance_utf8"])
            del metadata[field]
            reply["provenance_utf8"] = json.dumps(metadata)
            self.rejected(reply)
        reply = self.reply()
        metadata = json.loads(reply["provenance_utf8"])
        del metadata["source_atoms"][0]["isotope"]
        reply["provenance_utf8"] = json.dumps(metadata)
        self.rejected(reply)
        reply = self.reply()
        metadata = json.loads(reply["provenance_utf8"])
        metadata["source_bonds"] = [
            {
                "begin_atom_index": 0,
                "end_atom_index": 5,
                "bond_type": "SINGLE",
                "stereo": "STEREONONE",
                "stereo_atom_indices": [],
                "direction": "NONE",
            }
        ]
        reply["provenance_utf8"] = json.dumps(metadata)
        self.rejected(reply)

    def test_incomplete_stereo_annotations_and_boolean_native_settings_reject(self):
        for atom in ({"atom_index": 0}, {"atom_index": 0, "chiral_tag": "CHI_UNSPECIFIED", "cip_code": []}):
            reply = self.reply()
            annotation = json.loads(reply["conformers"][0]["stereochemistry_utf8"])
            annotation["atoms"] = [atom]
            reply["conformers"][0]["stereochemistry_utf8"] = json.dumps(annotation)
            self.rejected(reply)
        reply = self.reply()
        annotation = json.loads(reply["conformers"][0]["stereochemistry_utf8"])
        annotation["bonds"] = [{"begin_atom_index": 0, "end_atom_index": 1, "stereo_atom_indices": []}]
        reply["conformers"][0]["stereochemistry_utf8"] = json.dumps(annotation)
        self.rejected(reply)
        reply = self.reply()
        metadata = json.loads(reply["provenance_utf8"])
        metadata["embedding"]["requested_settings"]["numThreads"] = True
        reply["provenance_utf8"] = json.dumps(metadata)
        self.rejected(reply)

    def test_full_frame_boundary_and_overflow_becomes_an_operational_outcome(self):
        batch = collection()
        baseline = conformer_reply(batch, allocation="fixture")
        padding = MAX_FRAME_BYTES - len(frame(baseline)[4:])
        expanded = tuple(
            replace(item, request_bytes=item.request_bytes + b" " * (padding // 2 + (index < padding % 2)))
            for index, item in enumerate(batch.conformers)
        )
        exact = replace(batch, conformers=expanded)
        self.assertEqual(len(frame(conformer_reply(exact, allocation="fixture"))) - 4, MAX_FRAME_BYTES)
        # Valid JSON whitespace expands this authored transport fixture; it is
        # not a claim that native ETKDG emits a request of this size.
        overflow = replace(
            exact,
            conformers=(
                replace(exact.conformers[0], request_bytes=exact.conformers[0].request_bytes + b" "),
                exact.conformers[1],
            ),
        )
        reply = conformer_reply(overflow, allocation="fixture")
        self.assertEqual(reply["kind"], "conformer_output_limit")
        self.assertFalse(reply["coordinates_available"])
        with self.assertRaises(ConformerOutputLimit) as caught:
            prepared_conformers(reply, SOURCE, 2, **SETTINGS)
        evidence = json.loads(caught.exception.diagnostic)
        self.assertEqual(evidence, reply)
        self.assertEqual([item["conformer_id"] for item in evidence["conformers"]], [2, 0])
        self.assertNotIsInstance(caught.exception, InputRejected)
        self.assertNotIn("PRIVATE_NATIVE_DIAGNOSTIC", str(caught.exception))
        self.assertNotIn("PRIVATE_NATIVE_DIAGNOSTIC", repr(caught.exception))
        self.assertEqual(evidence["provenance_utf8"].encode(), overflow.provenance)
        self.assertEqual(evidence["diagnostic"], overflow.diagnostic)
        for record, item in zip(evidence["conformers"], overflow.conformers, strict=True):
            self.assertEqual(record["request_sha256"], hashlib.sha256(item.request_bytes).hexdigest())
            self.assertEqual(record["request_byte_length"], len(item.request_bytes))
            self.assertEqual(record["stereochemistry_sha256"], hashlib.sha256(item.stereochemistry).hexdigest())
            self.assertEqual(record["stereochemistry_byte_length"], len(item.stereochemistry))
            self.assertNotIn("request_utf8", record)
            self.assertNotIn("stereochemistry_utf8", record)

    def test_output_limit_of_an_incomplete_native_collection_is_not_a_shortfall_payload(self):
        batch = collection(requested=3)
        expanded = tuple(
            replace(item, request_bytes=item.request_bytes + b" " * (1024 * 1024 - len(item.request_bytes)))
            for item in batch.conformers
        )
        reply = conformer_reply(replace(batch, conformers=expanded), allocation="fixture")
        with self.assertRaises(ConformerOutputLimit) as caught:
            prepared_conformers(reply, SOURCE, 3, **SETTINGS)
        self.assertEqual(caught.exception.requested_count, 3)
        self.assertEqual(caught.exception.actual_count, 2)

    def test_tampered_output_limit_evidence_is_rejected_privately(self):
        batch = collection()
        expanded = tuple(
            replace(item, request_bytes=item.request_bytes + b" " * (1024 * 1024 - len(item.request_bytes)))
            for item in batch.conformers
        )
        original = conformer_reply(replace(batch, conformers=expanded), allocation="fixture")
        self.rejected(original | {"coordinates_available": True})
        for change in ({"request_sha256": "bad"}, {"conformer_id": True}, {"request_byte_length": True}):
            reply = copy.deepcopy(original)
            reply["conformers"][0].update(change)
            self.rejected(reply)
