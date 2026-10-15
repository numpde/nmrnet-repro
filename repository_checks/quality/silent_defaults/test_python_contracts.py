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

    def test_typed_pass_annotation_accepts_explicit_types_and_flat_tuples(self):
        for declared in (
            "OSError",
            "ipaddress.AddressValueError",
            "(FileNotFoundError, ProcessLookupError)",
            "(errors.Missing, OSError)",
        ):
            with self.subTest(declared=declared):
                source = "try:\n    inspect()\n" + self.annotation + "except " + declared + ":\n    pass\n"
                raw, applicable = self.exercise_property(source, accepted=True)
                self.assertEqual(1, len(raw))
                self.assertIn("python exception fallback:", raw[0])
                self.assertEqual([], applicable)

    def test_typed_pass_annotation_rejects_broad_computed_nested_and_nonpass_handlers(self):
        headers = (
            "except:",
            "except Exception:",
            "except BaseException:",
            "except builtins.Exception:",
            "except ExceptionGroup:",
            "except builtins.BaseExceptionGroup:",
            "except (OSError, Exception):",
            "except (OSError, builtins.BaseExceptionGroup):",
            "except ():",
            "except factory():",
            "except errors[0]:",
            "except args.failure:",
            "except (OSError, (ValueError,)):",
            "except (OSError, *errors):",
            "except* OSError:",
        )
        sources = ["try:\n    inspect()\n" + self.annotation + header + "\n    pass\n" for header in headers]
        sources.extend(
            (
                "try:\n    inspect()\n" + self.annotation + "except OSError:\n    return 0\n",
                "try:\n    inspect()\n" + self.annotation + 'except OSError:\n    pass\n    result = state.get("done", 0)\n',
            )
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_typed_pass_annotation_keeps_protected_operator_inputs_strict(self):
        statements = (
            'value = os.environ["PORT"]',
            'value = env["PORT"]',
            'value = os.getenv("PORT")',
            'value = os.environ.get("PORT", "8080")',
            "value = args[0]",
            "value = args.port",
            'parser.add_argument("--port", default="8080")',
            'parser.add_argument("profile", nargs="?")',
            'parser.add_argument("--port", required=True)',
            "value = parser.parse_args()",
            "value = parser.parse_known_args()",
        )
        for statement in statements:
            with self.subTest(statement=statement):
                source = "try:\n    " + statement + "\n" + self.annotation + "except KeyError:\n    pass\n"
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                if statement == statements[0]:
                    self.assertEqual(1, len(raw), msg="Required ENV access has only its swallowing-handler finding")
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_typed_pass_annotation_does_not_cover_neighbours_or_other_placements(self):
        source = 'try:\n    value = state.get("done", 0)\n' + self.annotation + "except OSError:\n    pass\n"
        raw, applicable = self.exercise_property(source, accepted=False)
        self.assertEqual(2, len(raw))
        self.assertEqual(1, len(applicable))
        self.assertIn('state.get("done", 0)', applicable[0])
        self.assertIn(applicable[0], raw)
        for tail, retained in (
            ('status = state.get("done", 0)\n', 'state.get("done", 0)'),
            ("except ValueError:\n    pass\n", "except ValueError: pass"),
        ):
            source = "try:\n    inspect()\n" + self.annotation + "except OSError:\n    pass\n" + tail
            raw, applicable = self.exercise_property(source, accepted=False)
            self.assertEqual(2, len(raw))
            self.assertEqual(1, len(applicable))
            self.assertIn(retained, applicable[0])
            self.assertIn(applicable[0], raw)
        sources = (
            self.annotation + "try:\n    inspect()\nexcept OSError:\n    pass\n",
            "try:\n    inspect()\nexcept OSError:\n    " + self.annotation + "    pass\n",
            "try:\n    inspect()\n    " + self.annotation + "except OSError:\n    pass\n",
            "try:\n    inspect()\n" + self.annotation + "\nexcept OSError:\n    pass\n",
            "try:\n    inspect()\nexcept OSError: " + self.annotation + "    pass\n",
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(1, len(raw))
                self.assertIn(raw[0], applicable)
                self.assertTrue(any("python contract annotation requires" in finding for finding in applicable))
        for body in ("raise", "return None"):
            with self.subTest(body=body):
                raw, applicable = self.exercise_property(
                    "try:\n    inspect()\n" + self.annotation + "except OSError:\n    " + body + "\n", accepted=False
                )
                self.assertEqual([], raw)
                self.assertEqual(1, len(applicable))
                self.assertIn("python contract annotation requires", applicable[0])

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

    def test_with_context_and_assignment_value_cover_one_multiline_generic_contract(self):
        sources = (
            'with os.fdopen(\n    descriptor,\n    "r+b" if flags & os.O_RDWR else "rb",\n    closefd=False,\n) as stream:\n    pass\n',
            'request = Request(\n    url,\n    headers={\n        **({"Range": f"bytes={offset}-"} if offset else {}),\n    },\n)\n',
            'path = (parsed.path\n        or "/")\n',
            'value = (\n    payload.get("value", 0) if ready else 0\n)\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=True)
                self.assertTrue(raw)
                self.assertEqual([], applicable)

    def test_with_and_assignment_regions_preserve_body_targets_and_adjacent_defaults(self):
        sources = (
            (
                'with acquire(mode if writable else "rb") as stream:\n    status = state.get("done", 0)\n',
                'state.get("done", 0)',
            ),
            (
                'with acquire(mode if writable else "rb") as targets[slots.get("slot", 0)]:\n    pass\n',
                'slots.get("slot", 0)',
            ),
            (
                'targets[slots.get("slot", 0)] = (\n    mode if writable else "rb"\n)\n',
                'slots.get("slot", 0)',
            ),
            (
                'value = (\n    mode if writable else "rb"\n)\nstatus = state.get("done", 0)\n',
                'state.get("done", 0)',
            ),
            (
                'with acquire(mode if writable else "rb"):\n    def inner(status=0):\n        pass\n',
                "python function default: 0",
            ),
        )
        for source, retained in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=False)
                self.assertEqual(2, len(raw))
                self.assertEqual(1, len(applicable))
                self.assertIn(retained, applicable[0])
                self.assertIn(applicable[0], raw)

    def test_with_and_assignment_regions_keep_multiline_operator_and_callable_inputs_strict(self):
        expressions = (
            'os.environ.get("PORT", "8080")',
            'payload.get("port", os.environ.get("PORT", "8080"))',
            'args[0] if args else "all"',
            "args.port or 443",
            'parser.add_argument("--port", default="8080")',
            'payload.get("port", parser.add_argument("--port", default="8080"))',
            'payload.get("callback", lambda status=0: status)',
        )
        for expression in expressions:
            for source in (
                "with acquire(\n    " + expression + "\n):\n    pass\n",
                "value = (\n    " + expression + "\n)\n",
            ):
                with self.subTest(source=source):
                    raw, applicable = self.exercise_property(self.annotation + source, accepted=False)
                    self.assertTrue(raw)
                    for finding in raw:
                        self.assertIn(finding, applicable)

    def test_with_and_assignment_regions_reject_siblings_and_other_statement_kinds(self):
        sources = (
            'with acquire(left.get("mode", "rb")), acquire(right.get("mode", "rb")):\n    pass\n',
            'value = (\n    left.get("value", 0),\n    right.get("value", 0),\n)\n',
            'value: object = (\n    payload.get("value", 0)\n)\n',
            'value += (\n    payload.get("value", 0)\n)\n',
            'async with acquire(\n    payload.get("value", 0)\n):\n    pass\n',
        )
        for source in sources:
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(self.annotation + source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)

    def test_nested_generic_expression_keeps_siblings_and_strict_findings_visible(self):
        expression = '{"CDCl3": 1, "DMSO-d6": 2}.get(solvent, 3) if solvent_present else 0'
        raw, applicable = self.exercise_property(self.annotation + "category = " + expression + "\n", accepted=True)
        self.assertEqual(2, len(raw))
        self.assertEqual([], applicable)
        for source in (
            self.annotation + 'values = (left.get("value", 0), right.get("value", 0))\n',
            self.annotation + 'value = payload.get("value", 0) if ready else os.environ.get("VALUE", "0")\n',
            self.annotation + 'value = payload.get("value", 0) if args.ready else 0\n',
            self.annotation + 'value = payload.get("value", 0) if ready else parser.add_argument("--value", default="0")\n',
            self.annotation + 'value = payload.get("value", parser.add_argument("--value", default="0")) if ready else 0\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)
        raw, applicable = self.exercise_property(
            self.annotation + "def run(value=" + expression + "):\n    pass\n", accepted=True
        )
        self.assertEqual(3, len(raw))
        self.assertEqual([], applicable)
        raw, applicable = self.exercise_property(
            "try:\n    run()\nexcept Exception:\n    " + self.annotation + "    return " + expression + "\n", accepted=False
        )
        strict = [finding for finding in raw if "python exception fallback:" in finding]
        self.assertTrue(strict)
        for finding in strict:
            self.assertIn(finding, applicable)

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

    def test_signature_contract_and_nested_exception_annotation_have_distinct_scopes(self):
        raw, applicable = self.exercise_property(
            self.annotation + "def run(port=parsed.port or 443):\n    return port\n", accepted=True
        )
        self.assertEqual(2, len(raw))
        self.assertEqual([], applicable)
        raw, applicable = self.exercise_property(
            "try:\n    run()\nexcept Exception:\n" + "    " + self.annotation + '    return payload.get("result", [])\n',
            accepted=False,
        )
        strict = [finding for finding in raw if "python exception fallback:" in finding]
        self.assertTrue(strict)
        for finding in strict:
            self.assertIn(finding, applicable)

    def test_signature_contract_covers_sync_async_and_multiline_defaults(self):
        for definition in (
            "def read(size=-1):\n    pass\n",
            "async def read(size=-1):\n    pass\n",
            "def read(\n    size=-1, /,\n    *, depth=0, optional=None,\n):\n    pass\n",
            "def read(size=payload.get(\n    'size', 17\n)):\n    pass\n",
        ):
            with self.subTest(definition=definition):
                raw, applicable = self.exercise_property(self.annotation + definition, accepted=True)
                self.assertTrue(raw)
                self.assertEqual([], applicable)

    def test_signature_contract_keeps_body_decorator_annotation_and_neighbours_visible(self):
        for outside in (
            '    return state.get("completed", 0)\n',
            "    def inner(status=0):\n        pass\n",
            "    pass\ndef other(status=0):\n    pass\n",
        ):
            with self.subTest(outside=outside):
                raw, applicable = self.exercise_property(self.annotation + "def read(size=-1):\n" + outside, accepted=False)
                self.assertEqual(2, len(raw))
                self.assertEqual(1, len(applicable))
                self.assertNotIn("-1", applicable[0])
        for source in (
            '@decorate(registry.get("decorator", object))\n' + self.annotation + "def read(size=-1):\n    pass\n",
            self.annotation + 'def read(size=-1) -> registry.get("Result", object):\n    pass\n',
            self.annotation + 'def read(size: registry.get("Size", int) = -1):\n    pass\n',
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertEqual(2, len(raw))
                self.assertEqual(1, len(applicable))
                self.assertIn("registry.get", applicable[0])

    def test_signature_contract_rejects_operator_defaults_and_wrong_boundaries(self):
        for expression in (
            'os.environ.get("PORT", "8080")',
            'os.environ["PORT"] or "8080"',
            'args[0] if args else "all"',
            "args.port or 443",
            'parser.add_argument("--port", default="8080")',
            'parser.add_argument("profile", nargs="?")',
            'payload.get("port", parser.add_argument("--port", default="8080"))',
            'lambda: state.get("completed", 0)',
        ):
            with self.subTest(expression=expression):
                raw, applicable = self.exercise_property(
                    self.annotation + "def run(value=" + expression + "):\n    pass\n", accepted=False
                )
                self.assertTrue(raw)
                for finding in raw:
                    self.assertIn(finding, applicable)
        for source in (
            "def record(status=0):\n    pass\n",
            self.annotation + 'def read(\n    size=-1,\n    port=os.environ.get("PORT", "8080"),\n):\n    pass\n',
            self.annotation + "def read(optional=None):\n    pass\n",
            self.annotation + "@decorate\ndef read(size=-1):\n    pass\n",
            self.annotation + "class Reader:\n    def read(self, size=-1):\n        pass\n",
        ):
            with self.subTest(source=source):
                raw, applicable = self.exercise_property(source, accepted=False)
                self.assertTrue(applicable)
                for finding in raw:
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
