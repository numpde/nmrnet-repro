"""Received source spelling may differ while the scientific oracle stays exact."""

from tools.container_guard import require_container

require_container()

import copy
import hashlib
import unittest

from api_prediction import compare_scientific_documents
from prediction_test_ports import scientific_documents


class ApiPredictionTests(unittest.TestCase):
    def test_real_whitespace_or_xyz_header_changes_only_received_source_evidence(self):
        for source_format in ("table", "xyz"):
            with self.subTest(source_format=source_format):
                actual, retained, receipt, metadata, interpreter, original = scientific_documents()
                received = (
                    "\n\n" + original.replace(" ", "   ") + "\n"
                    if source_format == "table"
                    else "5\nreceived XYZ comment\n" + original
                )
                source = actual["geometries"][0]["source"]
                source.update(
                    format=source_format,
                    sha256=hashlib.sha256(received.encode()).hexdigest(),
                    byte_length=len(received.encode()),
                    positions=[3, 4, 5, 6, 7],
                )
                if source_format == "xyz":
                    actual["geometries"][0]["preparation"]["method"] = "xyz"
                observed = copy.deepcopy(actual)
                compare_scientific_documents(actual, retained, receipt, metadata, interpreter=interpreter)
                self.assertEqual(actual, observed)

    def test_scripted_comparison_still_accepts_only_its_exact_source_and_science(self):
        actual, retained, receipt, metadata, _interpreter, _source = scientific_documents()
        compare_scientific_documents(actual, retained, receipt, metadata, interpreter=None)
