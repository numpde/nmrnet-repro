"""Frozen generated collections cross the protocol without regeneration."""

from tools.container_guard import require_container

require_container()

import sys
import unittest

from tests.unit.smiles_fakes import SETTINGS, SOURCE, collection

from nmrnet_provider.smiles import ConformerShortfall
from nmrnet_provider.smiles_protocol import conformer_reply, prepared_conformers, read_smiles_request, smiles_request
from nmrnet_provider.worker_protocol import document, frame


class SmilesProtocolNormativeTests(unittest.TestCase):
    def test_source_bytes_and_count_round_trip_without_work_budget_options(self):
        source = b" C "
        request = smiles_request(source, 2)
        self.assertEqual(set(request), {"operation", "smiles_utf8", "requested_count"})
        self.assertEqual(read_smiles_request(document(frame(request)[4:])), (source, 2))
        self.assertNotIn("rdkit", sys.modules)
        self.assertNotIn("torch", sys.modules)

    def test_exact_bytes_ids_and_private_metadata_round_trip_in_returned_order(self):
        expected = collection()
        reply = document(frame(conformer_reply(expected, allocation="fixture"))[4:])
        actual = prepared_conformers(reply, SOURCE, 2, **SETTINGS)
        self.assertEqual(actual, expected)
        self.assertEqual([item.conformer_id for item in actual.conformers], [2, 0])
        self.assertNotIn("PRIVATE_NATIVE_DIAGNOSTIC", repr(actual))
        self.assertNotIn("rdkit", sys.modules)

    def test_shortfall_keeps_the_whole_partial_and_zero_is_not_success(self):
        for identifiers in ((2, 0), ()):
            with self.subTest(identifiers=identifiers):
                partial = collection(identifiers, requested=3)
                reply = conformer_reply(partial, allocation="fixture")
                with self.assertRaises(ConformerShortfall) as caught:
                    prepared_conformers(reply, SOURCE, 3, **SETTINGS)
                self.assertEqual(caught.exception.partial, partial)
                self.assertNotIn("PRIVATE_NATIVE_DIAGNOSTIC", str(caught.exception))

    def test_encoder_refuses_more_conformers_than_the_requested_collection(self):
        with self.assertRaisesRegex(ValueError, "count"):
            conformer_reply(collection(requested=1), allocation="fixture")
