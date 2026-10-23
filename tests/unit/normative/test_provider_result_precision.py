"""Public shift precision leaves scientific inputs and raw outcomes unchanged."""

from tools.container_guard import require_container

require_container()

import copy
import json
import math
import unittest
from dataclasses import replace

from tests.unit.result_fakes import precision_documents

from nmrnet_provider.result import ResultPlan


class ResultPrecisionTests(unittest.TestCase):
    def test_default_four_digits_are_numeric_and_frozen_with_exact_inputs(self):
        prepared, facts, outcomes = precision_documents((32.309967041015625, 3.062568426132202, 0.001234567, -1.23456789, -0.0))
        original = copy.deepcopy(outcomes)
        plan = ResultPlan(prepared, **facts)
        document = json.loads(plan.finish(outcomes))
        self.assertEqual(document["output"], {"shift_significant_digits": 4, "verbosity": "medium"})
        self.assertEqual(json.loads(plan.prepared_identity)["output"], document["output"])
        for geometry, conformer in zip(document["geometries"], prepared.generated.conformers, strict=True):
            self.assertEqual(geometry["prepared_request"], json.loads(conformer.request_bytes))
            values = [row["shift_ppm"] for row in geometry["predictions"]]
            self.assertEqual(values, [32.31, 3.063, 0.001235, -1.235, 0.0])
            self.assertTrue(all(type(value) is float for value in values))
            self.assertEqual(math.copysign(1, values[-1]), 1)
        self.assertEqual(outcomes, original)
        self.assertLessEqual(len(plan.finish(outcomes)), plan.maximum_result_bytes)

    def test_significant_digits_round_binary_values_nearest_even_and_carry(self):
        prepared, facts, outcomes = precision_documents((1.25, 1.75, -1.25, 9.999, 0.000123456))
        for digits, expected in ((2, [1.2, 1.8, -1.2, 10.0, 0.00012]), (3, [1.25, 1.75, -1.25, 10.0, 0.000123])):
            with self.subTest(digits=digits):
                plan = ResultPlan(replace(prepared, output=replace(prepared.output, shift_significant_digits=digits)), **facts)
                document = json.loads(plan.finish(outcomes))
                self.assertEqual([row["shift_ppm"] for row in document["geometries"][0]["predictions"]], expected)

    def test_seventeen_digits_preserve_binary64_values_and_reserve_unrounded_evidence(self):
        values = (32.309967041015625, -1.7976931348623157e308, 5e-324, float(2**1023), 0.0)
        prepared, facts, outcomes = precision_documents(values)
        plan = ResultPlan(replace(prepared, output=replace(prepared.output, shift_significant_digits=17)), **facts)
        document = json.loads(plan.finish(outcomes))
        self.assertEqual([row["shift_ppm"] for row in document["geometries"][0]["predictions"]], list(values))
        reservation = plan.scientific_evidence_reservation()
        self.assertEqual(len(reservation), len(outcomes))
        for reserved, observed in zip(reservation, outcomes, strict=True):
            self.assertEqual(reserved["request_sha256"], observed["request_sha256"])
            self.assertEqual(reserved["provenance"], observed["provenance"])
            self.assertEqual(reserved["private_warnings"], [])
            self.assertEqual(len(reserved["prediction"]["predictions"]), len(observed["prediction"]["predictions"]))
            for row in reserved["prediction"]["predictions"]:
                self.assertEqual(row["shift_ppm"], "x" * 32)
        self.assertGreaterEqual(len(json.dumps(reservation)), len(json.dumps(outcomes)))
