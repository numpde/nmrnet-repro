"""Invalid output fragments and malformed science cannot become low-verbosity success."""

from tools.container_guard import require_container

require_container()

import unittest
from dataclasses import replace

from tests.unit.result_fakes import supplied_documents

from nmrnet_provider.prepared_input import DEFAULT_OUTPUT, OutputSettings
from nmrnet_provider.result import ResultPlan, ResultRejected
from provider_core.result_verbosity import ResultVerbosity


class ResultVerbosityRejectionTests(unittest.TestCase):
    def test_fragment_defaults_are_shared_and_only_exact_choices_are_admitted(self):
        self.assertEqual(OutputSettings.from_json({}), DEFAULT_OUTPUT)
        for value in (None, True, 0, "", "compact", "full", "MAX", " max", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "verbosity.*low.*medium.*max"):
                OutputSettings.from_json({"verbosity": value})
        for value in (None, [], "low", {"verbosity": "low", "unknown": True}):
            with self.subTest(fragment=value), self.assertRaisesRegex(ValueError, "output.*object"):
                OutputSettings.from_json(value)

    def test_scientific_admission_precedes_every_public_projection(self):
        for verbosity in ResultVerbosity:
            with self.subTest(verbosity=verbosity):
                prepared, facts, outcomes = supplied_documents()
                plan = ResultPlan(replace(prepared, output=replace(prepared.output, verbosity=verbosity)), **facts)
                outcomes[0]["prediction"]["predictions"][0]["atom_index"] = 1
                with self.assertRaisesRegex(ResultRejected, "prepared atom order"):
                    plan.finish(outcomes)
