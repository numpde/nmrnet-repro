"""Keep reviewed generic contracts local while raw and strict findings survive.

Each case invokes the actual repository property over an isolated authored file.
A proof reference must exist; its meaning remains review evidence, not a machine
proof of consumer semantics or permission for arbitrary proof-state data.
"""

from tools.container_guard import require_container

require_container()

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import test_python as policy


class PythonContractApplicabilityTests(unittest.TestCase):
    def setUp(self):
        self.annotation = (
            "# silent-defaults: allow-contract -- The protocol requires an origin path; "
            "proof=tests/proof.py::ProofTests.test_origin\n"
        )
        self.proof_source = "class ProofTests:\n    def test_origin(self):\n        pass\n"
        self.proof_path_state = "regular"

    def exercise_property(self, source, *, accepted):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "tools/consumer.py"
            path.parent.mkdir()
            path.write_text(source)
            proof = root / "tests/proof.py"
            proof.parent.mkdir()
            if self.proof_source is not None:
                proof.write_text(self.proof_source)
            if self.proof_path_state == "leaf_link":
                backing = proof.rename(root / "proof-backing.py")
                proof.symlink_to(backing)
            elif self.proof_path_state == "ancestor_link":
                backing = proof.parent.rename(root / "proof-backing")
                proof.parent.symlink_to(backing, target_is_directory=True)
            with patch.object(policy, "ROOT", root), patch.object(policy, "python_files", return_value=[path]):
                raw = policy.python_default_findings(path)
                applicable = policy.python_operator_default_findings(path)
                property_test = policy.PythonSilentDefaultsTest()
                if accepted:
                    property_test.test_python_files_do_not_default_operator_inputs()
                else:
                    with self.assertRaises(AssertionError):
                        property_test.test_python_files_do_not_default_operator_inputs()
            return raw, applicable

    def test_if_header_annotation_covers_one_expression_and_preserves_body_findings(self) -> None:
        header = 'if response.headers.get("Content-Encoding", "identity") != "identity":\n'
        raw, applicable = self.exercise_property(self.annotation + header + '    raise ValueError("encoded")\n', accepted=True)
        self.assertEqual(1, len(raw))
        self.assertEqual([], applicable)
        raw, applicable = self.exercise_property(
            self.annotation + header + '    status = state.get("completed", 0)\n', accepted=False
        )
        self.assertEqual(2, len(raw))
        self.assertEqual([raw[1]], applicable)

    def test_if_header_annotation_rejects_multiline_and_multiple_candidates(self) -> None:
        sources = (
            self.annotation + 'if response.headers.get(\n    "Content-Encoding", "identity"\n) != "identity":\n    pass\n',
            self.annotation
            + 'if (response.headers.get("Content-Encoding", "identity") != "identity"\n    and enabled):\n    pass\n',
            self.annotation + 'if left.get("coding", "identity") == right.get("coding", "identity"):\n    pass\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_header_extension_keeps_operator_and_non_if_contracts_strict(self) -> None:
        sources = (
            self.annotation + 'if os.environ.get("PORT", "8080"):\n    pass\n',
            self.annotation + "if args.port or 443:\n    pass\n",
            self.annotation + 'if parser.add_argument("--port", default="8080"):\n    pass\n',
            self.annotation + 'def run() -> registry.get("Result", object):\n    pass\n',
            self.annotation + 'for item in payload.get("items", []):\n    pass\n',
            self.annotation + 'while parsed.path or "/":\n    pass\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_local_review_accepts_generic_candidates_without_changing_raw_findings(self):
        for expression in ('parsed.path or "/"', 'payload.get("context", {})', 'value if value is not None else "rendered"'):
            with self.subTest(expression=expression):
                raw, applicable = self.exercise_property(self.annotation + "value = " + expression + "\n", accepted=True)
                self.assertEqual(1, len(raw))
                self.assertEqual([], applicable)

    def test_annotation_cannot_suppress_environment_or_cli_rules(self):
        expressions = (
            'os.environ.get("PORT", "8080")',
            'os.environ["PORT"] or "8080"',
            'env["PORT"] if "PORT" in env else "8080"',
            'args[0] if args else "all"',
            "args.port or 443",
            'parser.add_argument("--port", default="8080")',
            'parser.add_argument("profile", nargs="?")',
            'parser.add_argument("--port", default=parsed.port or 443)',
        )
        for expression in expressions:
            with self.subTest(expression=expression):
                raw, applicable = self.exercise_property(self.annotation + "value = " + expression + "\n", accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_function_and_exception_findings_survive_nested_generic_annotations(self):
        sources = (
            self.annotation + "def run(port=parsed.port or 443):\n    return port\n",
            "try:\n    run()\nexcept Exception:\n" + "    " + self.annotation + '    return payload.get("result", [])\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                strict = [
                    finding for finding in raw if "python function default:" in finding or "python exception fallback:" in finding
                ]
                self.assertTrue(strict)
                for finding in strict:
                    self.assertIn(finding, applicable)

    def test_bad_or_ambiguous_annotations_do_not_admit_a_candidate(self):
        statement = 'path = parsed.path or "/"\n'
        sources = (
            self.annotation.replace("The protocol requires an origin path", " ") + statement,
            self.annotation.replace("; proof=", "; missing=") + statement,
            self.annotation.replace("tests/proof.py::ProofTests.test_origin", "missing") + statement,
            self.annotation.replace("tests/proof.py::", "tests/../proof.py::") + statement,
            self.annotation.replace("tests/proof.py::", "/tests/proof.py::") + statement,
            self.annotation + "\n" + statement,
            statement.rstrip() + "  " + self.annotation,
            "annotation = " + repr(self.annotation) + "\n" + statement,
            self.annotation + 'paths = (left or "/", right or "/")\n',
            self.annotation + 'path = parsed.path or "/"; other = required\n',
            self.annotation + 'path = (parsed.path\n        or "/")\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_stale_annotation_is_a_finding_even_without_a_raw_default(self):
        raw, applicable = self.exercise_property(self.annotation + "value = required\n", accepted=False)
        self.assertEqual([], raw)
        self.assertEqual(1, len(applicable))
        self.assertIn("python contract annotation requires", applicable[0])

    def test_accepting_one_candidate_preserves_unannotated_neighbouring_findings(self):
        source = self.annotation + 'path = parsed.path or "/"\nstatus = state.get("completed", 0)\n'
        raw, applicable = self.exercise_property(source, accepted=False)
        self.assertEqual(2, len(raw))
        self.assertEqual([raw[1]], applicable)

    def test_missing_linked_malformed_or_ambiguous_proof_targets_do_not_admit(self):
        sources = (
            None,
            "not valid Python !\n",
            "class OtherTests:\n    def test_origin(self):\n        pass\n",
            "class ProofTests:\n    def other(self):\n        pass\n",
            self.proof_source + self.proof_source,
            self.proof_source + "    def test_origin(self):\n        pass\n",
            self.proof_source + "    async def test_origin(self):\n        pass\n",
            "def nested():\n    class ProofTests:\n        def test_origin(self):\n            pass\n",
        )
        for proof_source in sources:
            with self.subTest(proof_source=proof_source):
                self.proof_source = proof_source
                raw, applicable = self.exercise_property(self.annotation + 'path = parsed.path or "/"\n', accepted=False)
                self.assertEqual(1, len(raw))
                self.assertIn(raw[0], applicable)
        self.proof_source = "class ProofTests:\n    def test_origin(self):\n        pass\n"
        for state in ("leaf_link", "ancestor_link"):
            with self.subTest(state=state):
                self.proof_path_state = state
                raw, applicable = self.exercise_property(self.annotation + 'path = parsed.path or "/"\n', accepted=False)
                self.assertEqual(1, len(raw))
                self.assertIn(raw[0], applicable)
        self.proof_path_state = "regular"
        self.proof_source = "class ProofTests:\n    def origin(self):\n        pass\n"
        raw, applicable = self.exercise_property(
            self.annotation.replace("ProofTests.test_origin", "ProofTests.origin") + 'path = parsed.path or "/"\n',
            accepted=False,
        )
        self.assertEqual(1, len(raw))
        self.assertIn(raw[0], applicable)


if __name__ == "__main__":
    unittest.main()
