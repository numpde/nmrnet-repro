"""An archive claim cannot substitute another Job or another member's digest."""

import unittest

from tests.unit.result_fakes import scientific_facts

from nmrnet_provider.input_geometry import UploadMemberSource, UploadSource, prepare_table
from nmrnet_provider.prepared_input import PreparedJobInput
from nmrnet_provider.result import ResultPlan


class UploadedMemberResultRefusalTests(unittest.TestCase):
    def test_other_job_or_member_digest_cannot_be_joined_to_reader_geometry(self):
        geometry = prepare_table(b"C 0 0 0\n", source_unit="angstrom")
        for job_ref, member_hash in (("job:other", geometry.source_sha256), ("job:fixture", "f" * 64)):
            source = UploadMemberSource(UploadSource(job_ref, "upload:fixture", 100, "a" * 64), "one.xyz", 8, member_hash)
            prepared = PreparedJobInput((geometry,), "coordinate_table", source)
            with self.subTest(job_ref=job_ref), self.assertRaisesRegex(ValueError, "verified source"):
                ResultPlan(prepared, **scientific_facts())
