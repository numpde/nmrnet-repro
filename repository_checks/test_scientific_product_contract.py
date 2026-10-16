"""Bind copied solvent vocabulary and missingness to pinned upstream source.

Crosswalk: nmrpeak-repro f8055ab1065795fd46f8c1539e894cf9773f5495,
tests/repository/test_nmrpeak_product_contract.py::
test_multiplicities_equal_the_pinned_tokenizer_vocabulary. NMRNet copies solvent
categories rather than multiplicity tokens. AST reads their actual upstream
declarations; the authored selector then executes against every category,
unknown values, explicit None and a missing key without scientific imports.
https://github.com/numpde/nmrpeak-repro/blob/f8055ab1065795fd46f8c1539e894cf9773f5495/tests/repository/test_nmrpeak_product_contract.py
"""

from tools.container_guard import require_container

require_container()

import ast
import pickle
import unittest

from repository_checks.quality.sources import ROOT
from repository_checks.science.source import regular_bytes
from tools import baseline_prepare


def upstream_solvent_contract(path):
    tree = ast.parse(regular_bytes(path).decode("utf-8"))
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SolventDataset")
    initializer = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    argument_defaults = dict(
        zip(
            (argument.arg for argument in initializer.args.args[-len(initializer.args.defaults) :]),
            initializer.args.defaults,
            strict=True,
        )
    )
    categories = ast.literal_eval(argument_defaults["solvent_map"])
    getter = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "__getitem__")
    fallback = next(
        node
        for node in ast.walk(getter)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "solvent_map"
    )
    unknown = ast.literal_eval(fallback.args[1])
    handler = next(
        node
        for node in ast.walk(getter)
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) and node.type.id == "KeyError"
    )
    missing = next(
        ast.literal_eval(node.value)
        for node in handler.body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "id" for target in node.targets)
    )
    if not categories:
        raise ValueError("Pinned upstream solvent vocabulary is empty")
    return categories, unknown, missing


class ScientificProductContractTests(unittest.TestCase):
    def test_solvent_categories_unknown_none_and_missing_equal_pinned_source(self):
        categories, unknown, missing = upstream_solvent_contract(ROOT / "NMRNetplusplus/uninmr/data/key_dataset.py")
        cases = [(name, category, True) for name, category in categories.items()]
        # silent-defaults: allow-contract -- Present None uses the pinned solvent map or its unknown category; the separate missing-key case preserves upstream missingness; proof=repository_checks/test_scientific_product_contract.py::ScientificProductContractTests.test_solvent_categories_unknown_none_and_missing_equal_pinned_source
        cases.extend(
            [(None, categories.get(None, unknown), True), ("unrecognized-solvent", unknown, True), (None, missing, False)]
        )
        for solvent, expected, present in cases:
            with self.subTest(solvent=solvent, present=present):
                record = {
                    "atoms": ["C", "H"],
                    "coordinates": [[0.0, 0.0, 0.0], [0.0, 0.0, 1.0]],
                    "atoms_target": [1.0, 2.0],
                    "atoms_target_mask": [0, 1],
                }
                if present:
                    record["nmr_solvent"] = solvent
                _, receipts = baseline_prepare.select_records(
                    [(b"source", pickle.dumps(record))],
                    element="H",
                    dictionary_tokens=["C", "H"],
                    count=1,
                    max_atoms=2,
                    max_tokens=4,
                )
                self.assertEqual(expected, receipts[0]["effective_solvent_category"])
                self.assertEqual(present, receipts[0]["solvent_present"])
