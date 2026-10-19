"""Incomplete generation and oversized provenance cannot become a result plan."""

from tools.container_guard import require_container

require_container()

import json
import unittest
from dataclasses import replace

from tests.unit.result_fakes import scientific_facts, scientific_outcomes
from tests.unit.smiles_fakes import collection

from nmrnet_provider.result import MAX_RESULT_BYTES, ResultPlan, ResultRejected


class GeneratedResultRejectionTests(unittest.TestCase):
    def test_shortfall_and_empty_collection_cannot_plan_partial_success(self):
        for identifiers in ((2,), ()):
            with self.subTest(identifiers=identifiers), self.assertRaisesRegex(ResultRejected, "complete"):
                ResultPlan(collection(identifiers, requested=2), **scientific_facts())

    def test_shared_provenance_counts_against_the_whole_result_limit(self):
        batch = collection()
        metadata = json.loads(batch.provenance)
        metadata["model_projection"] = "x" * MAX_RESULT_BYTES
        with self.assertRaisesRegex(ResultRejected, "before inference"):
            ResultPlan(replace(batch, provenance=json.dumps(metadata).encode()), **scientific_facts())

    def test_reordered_or_missing_outcomes_cannot_publish_a_prefix(self):
        batch, facts = collection(), scientific_facts()
        plan = ResultPlan(batch, **facts)
        outcomes = scientific_outcomes(batch, facts)
        for changed in (outcomes[::-1], outcomes[:1]):
            with self.subTest(count=len(changed)), self.assertRaises(ResultRejected):
                plan.finish(changed)

    def test_a_generated_collection_does_not_bypass_required_model_selection(self):
        facts = scientific_facts()
        del facts["selected_artifacts"]["elements"]["H"]
        with self.assertRaisesRegex(ValueError, "required model selection"):
            ResultPlan(collection(), **facts)
