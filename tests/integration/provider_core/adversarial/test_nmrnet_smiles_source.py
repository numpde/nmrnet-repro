"""Malformed selections cannot create a source for native generation."""

import unittest

from tests.fakes.job_input import facts, response

from nmrnet_provider.input_geometry import InputRejected
from nmrnet_provider.submission import select_job_smiles
from provider_core.job_input import verify_job_input


class SmilesSourceRejectionTests(unittest.TestCase):
    def setUp(self):
        self.verified = verify_job_input(response(b"CCl"), feed=facts(b"CCl"))

    def test_unverified_input_is_an_implementation_error(self):
        with self.assertRaisesRegex(TypeError, "verified"):
            select_job_smiles(object(), smiles="CCl", requested_count=1)

    def test_invalid_text_and_byte_limit_are_checked_without_coercion(self):
        for smiles in (None, b"CCl", "", "\ud800", "C" * (1024 * 1024 + 1)):
            with self.subTest(kind=type(smiles).__name__), self.assertRaises(InputRejected):
                select_job_smiles(self.verified, smiles=smiles, requested_count=1)

    def test_selected_count_is_explicit_bounded_and_never_coerced(self):
        for count in (True, 0, 129, 1.5, "2", None):
            with self.subTest(count=count), self.assertRaisesRegex(InputRejected, "count"):
                select_job_smiles(self.verified, smiles="CCl", requested_count=count)

    def test_multiple_tokens_are_rejected_without_echoing_source(self):
        with self.assertRaises(InputRejected) as caught:
            select_job_smiles(self.verified, smiles="CCl PRIVATE_TOKEN", requested_count=1)
        self.assertNotIn("PRIVATE_TOKEN", str(caught.exception))
