"""A chooser selects original Job bytes without transcribing molecular text."""

import hashlib
import unittest
from dataclasses import FrozenInstanceError

from tests.fakes.job_input import facts, response

from nmrnet_provider.submission import select_job_smiles
from provider_core.job_input import verify_job_input


class SmilesSourceTests(unittest.TestCase):
    def test_utf8_byte_span_preserves_exact_source_and_job_identity(self):
        prefix = "Résumé: predict shifts from SMILES ".encode()
        source = b" [13CH3]Cl "
        raw = prefix + source + b"; generate three conformers."
        verified = verify_job_input(response(raw), feed=facts(raw))
        selected = select_job_smiles(verified, start_byte=len(prefix), end_byte=len(prefix) + len(source), requested_count=3)
        self.assertEqual(selected.job_ref, verified.job_ref)
        self.assertEqual(selected.input_fingerprint, verified.identity.input_fingerprint)
        self.assertEqual((selected.start_byte, selected.end_byte), (len(prefix), len(prefix) + len(source)))
        self.assertEqual(selected.source_bytes, source)
        self.assertEqual(selected.source_sha256, hashlib.sha256(source).hexdigest())
        self.assertEqual(selected.requested_count, 3)
        self.assertNotIn("[13CH3]Cl", repr(selected))
        with self.assertRaises(FrozenInstanceError):
            selected.requested_count = 2

    def test_repeated_tokens_are_distinguished_by_the_selected_span(self):
        raw = b"Ignore CCl here; use CCl there; N=2."
        selected = select_job_smiles(
            verify_job_input(response(raw), feed=facts(raw)), start_byte=21, end_byte=24, requested_count=2
        )
        self.assertEqual(selected.source_bytes, b"CCl")
        self.assertEqual((selected.start_byte, selected.end_byte), (21, 24))
