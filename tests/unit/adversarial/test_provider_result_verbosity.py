"""Invalid output fragments and malformed science cannot become low-verbosity success."""

from tools.container_guard import require_container

require_container()

import unittest
from dataclasses import replace

from tests.unit.result_fakes import supplied_documents

from nmrnet_provider.prepared_input import MIN_SHIFT_SIGNIFICANT_DIGITS, MAX_SHIFT_SIGNIFICANT_DIGITS, DEFAULT_OUTPUT, OutputSettings
from nmrnet_provider.result import ResultPlan, ResultRejected
from provider_core.result_verbosity import ResultVerbosity


class ResultVerbosityRejectionTests(unittest.TestCase):
    def test_fragment_defaults_are_shared_and_only_exact_choices_are_admitted(self):
        for (fragment, expected) in (
            ({}, DEFAULT_OUTPUT),
            ({"verbosity": "low"}, OutputSettings(verbosity=ResultVerbosity.LOW)),
            ({"shift_significant_digits": MAX_SHIFT_SIGNIFICANT_DIGITS}, OutputSettings(shift_significant_digits=MAX_SHIFT_SIGNIFICANT_DIGITS)),
            ({"verbosity": "max", "shift_significant_digits": MAX_SHIFT_SIGNIFICANT_DIGITS},
             OutputSettings(verbosity=ResultVerbosity.MAX, shift_significant_digits=MAX_SHIFT_SIGNIFICANT_DIGITS)),
        ):
            with self.subTest(fragment=fragment):
                self.assertEqual(expected, OutputSettings.from_json(fragment))
        for (value, error) in ((None, TypeError), (True, TypeError), (MIN_SHIFT_SIGNIFICANT_DIGITS - 1, ValueError), (MAX_SHIFT_SIGNIFICANT_DIGITS + 1, ValueError)):
            with self.subTest(precision=value), self.assertRaises(error):
                OutputSettings.from_json({"shift_significant_digits": value})
        for value in (None, [], "low"):
            with self.subTest(fragment=value), self.assertRaises(TypeError):
                OutputSettings.from_json(value)

    def test_unknown_fields_warn_without_changing_supplied_settings(self):
        fragment = {"verbosity": "low", "unknown": "PRIVATE_CANARY"}
        with self.assertLogs("nmrnet_provider.prepared_input", level="WARNING") as captured:
            settings = OutputSettings.from_json(fragment)
        self.assertEqual(OutputSettings(verbosity=ResultVerbosity.LOW), settings)
        self.assertIn("unknown", captured.output[0])
        self.assertNotIn("PRIVATE_CANARY", captured.output[0])
        self.assertEqual({"verbosity": "low", "unknown": "PRIVATE_CANARY"}, fragment)
        self.assertIs(str, type(fragment["verbosity"]))

    def test_malformed_settings_remain_input_rejections(self):
        from nmrnet_provider.input_geometry import InputRejected
        from nmrnet_provider.input_preparation import _selected_output

        fragments = [None, {"shift_significant_digits": True}, {"shift_significant_digits": MIN_SHIFT_SIGNIFICANT_DIGITS - 1}]
        fragments.extend({"verbosity": value} for value in (None, True, 0, "", "compact", "full", "MAX", " max", [], {}))
        for fragment in fragments:
            with self.subTest(fragment=fragment):
                with self.assertRaises(InputRejected) as caught:
                    _selected_output(fragment)
                self.assertIsInstance(caught.exception.__cause__, (TypeError, ValueError))

    def test_scientific_admission_precedes_every_public_projection(self):
        for verbosity in ResultVerbosity:
            with self.subTest(verbosity=verbosity):
                prepared, facts, outcomes = supplied_documents()
                plan = ResultPlan(replace(prepared, output=replace(prepared.output, verbosity=verbosity)), **facts)
                outcomes[0]["prediction"]["predictions"][0]["atom_index"] = 1
                with self.assertRaisesRegex(ResultRejected, "prepared atom order"):
                    plan.finish(outcomes)
