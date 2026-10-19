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
            select_job_smiles(object(), start_byte=0, end_byte=3, requested_count=1)

    def test_span_bounds_and_types_are_checked_without_coercion(self):
        for start, end in ((-1, 2), (0, 4), (1, 1), (2, 1), (True, 3), (0, False), (0.0, 3), (0, "3")):
            with self.subTest(start=start, end=end), self.assertRaisesRegex(InputRejected, "byte span"):
                select_job_smiles(self.verified, start_byte=start, end_byte=end, requested_count=1)

    def test_selected_count_is_explicit_bounded_and_never_coerced(self):
        for count in (True, 0, 129, 1.5, "2", None):
            with self.subTest(count=count), self.assertRaisesRegex(InputRejected, "count"):
                select_job_smiles(self.verified, start_byte=0, end_byte=3, requested_count=count)

    def test_split_utf8_or_multiple_tokens_are_rejected_without_echoing_source(self):
        for raw, start, end in (("é CCl".encode(), 1, 2), (b"CCl PRIVATE_TOKEN", 0, 17)):
            verified = verify_job_input(response(raw), feed=facts(raw))
            with self.subTest(start=start), self.assertRaises(InputRejected) as caught:
                select_job_smiles(verified, start_byte=start, end_byte=end, requested_count=1)
            self.assertNotIn("PRIVATE_TOKEN", str(caught.exception))
