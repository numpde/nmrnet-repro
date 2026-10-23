"""Precision cannot admit invalid raw values or manufacture nonfinite output."""

from tools.container_guard import require_container

require_container()

import unittest
from dataclasses import replace

from tests.unit.result_fakes import precision_documents

from nmrnet_provider.result import ResultPlan, ResultRejected


class ResultPrecisionRejectionTests(unittest.TestCase):
    def test_precision_requires_an_integer_from_one_through_seventeen(self):
        prepared, facts, _outcomes = precision_documents((1.0,) * 5)
        for digits in (0, 18, True, 4.0, "4", None):
            with self.subTest(digits=digits), self.assertRaisesRegex(ValueError, "integer.*1.*17"):
                ResultPlan(replace(prepared, output=replace(prepared.output, shift_significant_digits=digits)), **facts)

    def test_raw_exact_binary64_admission_precedes_rounding(self):
        prepared, facts, outcomes = precision_documents((2**53 + 1, 1.0, 1.0, 1.0, 1.0))
        plan = ResultPlan(replace(prepared, output=replace(prepared.output, shift_significant_digits=1)), **facts)
        with self.assertRaisesRegex(ResultRejected, "exactly representable"):
            plan.finish(outcomes)

    def test_rounding_a_finite_input_beyond_binary64_capacity_is_rejected(self):
        prepared, facts, outcomes = precision_documents((1.7976931348623157e308, 1.0, 1.0, 1.0, 1.0))
        plan = ResultPlan(replace(prepared, output=replace(prepared.output, shift_significant_digits=1)), **facts)
        with self.assertRaisesRegex(ResultRejected, "round.*finite"):
            plan.finish(outcomes)
