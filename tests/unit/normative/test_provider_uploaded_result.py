"""Public and durable identities distinguish an Upload ZIP from consumed member bytes."""

import hashlib
import json
import unittest
from types import SimpleNamespace

from tests.unit.result_fakes import scientific_facts, scientific_outcomes

from nmrnet_provider.input_geometry import UploadMemberSource, UploadSource
from nmrnet_provider.prepared_input import PreparedJobInput
from nmrnet_provider.result import ResultPlan
from nmrnet_provider.xyz import prepare_xyz


class UploadedMemberResultTests(unittest.TestCase):
    def test_member_and_archive_identities_survive_result_and_prepared_identity(self):
        raw = b"2\nfirst\nC 0 0 0\nH 1.09 0 0\n2\nsecond\nC 1.5 0 0\nH 2.59 0 0\n"
        geometries = prepare_xyz(raw, source_unit="angstrom", selection=(2, 1))
        upload = UploadSource("job:fixture", "upload:sha256:" + "a" * 64, 300, "b" * 64)
        source = UploadMemberSource(upload, "molecules/two.xyz", len(raw), hashlib.sha256(raw).hexdigest())
        prepared = PreparedJobInput(geometries, "xyz", source, shift_significant_digits=17)
        facts = scientific_facts()
        plan = ResultPlan(prepared, **facts)
        outcome = plan.finish(scientific_outcomes(SimpleNamespace(conformers=geometries), facts))
        for document in (json.loads(plan.prepared_identity), json.loads(outcome)):
            self.assertEqual([2, 1], [row["source"]["record_number"] for row in document["geometries"]])
            for geometry in document["geometries"]:
                self.assertEqual(
                    geometry["source"]["archive"],
                    {"sha256": upload.sha256, "byte_length": upload.byte_length, "member_name": source.member_name},
                )
                self.assertEqual(geometry["source"]["sha256"], source.sha256)
                self.assertEqual(geometry["source"]["byte_length"], len(raw))
                self.assertEqual(geometry["source"]["upload_ref"], upload.upload_ref)
                self.assertNotEqual(geometry["source"]["sha256"], geometry["source"]["archive"]["sha256"])
