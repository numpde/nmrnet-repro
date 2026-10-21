"""Received source spelling may differ while the scientific oracle stays exact."""

from tools.container_guard import require_container

require_container()

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from api_prediction import compare_scientific_documents, receive, submit
from prediction_test_ports import clocked_client, scientific_documents


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

    def test_delayed_result_crosses_old_reader_and_process_limits_without_another_job(self):
        with tempfile.TemporaryDirectory() as temporary:
            client, clock, calls = clocked_client(Path(temporary), ready_at=601)
            receipt = submit(client, {"specification": "Fixed supplied coordinates in nm"})
            created = (client.output / "created.json").read_bytes()
            with (
                patch("api_prediction.time.monotonic", clock.monotonic),
                patch("api_prediction.time.sleep", clock.sleep),
                patch("api_prediction.compare_science") as compare,
            ):
                receive(client, receipt, readback_seconds=900)
            self.assertEqual(clock.seconds, 601)
            self.assertEqual((client.output / "created.json").read_bytes(), created)
            self.assertEqual(json.loads(created), receipt)
            self.assertTrue((client.output / "scientific-result.json").exists())
            self.assertEqual([method for _label, method, *_rest in calls if method != "GET"], ["POST", "PUT", "PUT"])
            compare.assert_called_once()
