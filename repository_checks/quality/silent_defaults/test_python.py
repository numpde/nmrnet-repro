# Port source: https://github.com/numpde/dapp32/blob/778c16f8d8b77181d2ac4cd11a62a2034ded2518/tests/checks/silent_defaults/test_python.py
# Original detector semantics and fixtures retained; inventories use authored NMRNet paths.
from __future__ import annotations

from tools.container_guard import require_container

require_container()

import ast
import io
import re
import tokenize
import unittest
from collections.abc import Callable, Iterator
from pathlib import Path

from ..sources import ROOT
from .test_shared_scanner import repo_files

ENV_NAME_FRAGMENTS = ("env", "environ", "process_env")
DEFAULTING_CALL_NAMES = {"get", "getenv", "pop", "setdefault"}
ARGPARSE_CALL_NAMES = {"add_argument"}


def python_files() -> list[Path]:
    return repo_files(("**/*.py",))


def is_env_mapping(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return any(fragment in node.id.lower() for fragment in ENV_NAME_FRAGMENTS)
    return (
        isinstance(node, ast.Attribute) and node.attr == "environ" and isinstance(node.value, ast.Name) and node.value.id == "os"
    )


def is_defaulting_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in DEFAULTING_CALL_NAMES:
        return False
    if node.func.attr == "getenv":
        return isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
    return is_env_mapping(node.func.value)


def is_env_subscript(node: ast.AST) -> bool:
    return isinstance(node, ast.Subscript) and is_env_mapping(node.value)


def is_explicit_defaulting_call(node: ast.AST) -> bool:
    if not is_defaulting_call(node):
        return False
    assert isinstance(node, ast.Call)
    if isinstance(node.func, ast.Attribute) and node.func.attr == "setdefault":
        return True
    return len(node.args) >= 2 or any(keyword.arg == "default" for keyword in node.keywords)


def is_explicit_mapping_default_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in DEFAULTING_CALL_NAMES:
        return False
    if is_defaulting_call(node):
        return False
    if node.func.attr == "setdefault":
        return True
    return len(node.args) >= 2 or any(keyword.arg == "default" for keyword in node.keywords)


def contains_env_access(node: ast.AST) -> bool:
    return any(is_defaulting_call(child) or is_env_subscript(child) for child in ast.walk(node))


def is_env_validation_predicate(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return contains_env_access(node.operand) or is_env_validation_predicate(node.operand)
    if isinstance(node, ast.Compare):
        return any(is_env_mapping(comparator) for comparator in node.comparators)
    return False


def is_env_or_default_expression(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and any(contains_env_access(value) for value in node.values)
        and any(not is_env_validation_predicate(value) for value in node.values)
    )


def is_absence_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value in {"", None}


def is_default_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant | ast.List | ast.Tuple | ast.Dict | ast.Set)


def is_non_empty_default_value(node: ast.AST) -> bool:
    return not contains_env_access(node) and not is_absence_literal(node)


def is_env_default_if_expression(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.IfExp)
        and not is_absence_literal(node.orelse)
        and (contains_env_access(node.body) or contains_env_access(node.test))
    )


def is_generic_or_default_expression(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.BoolOp)
        and isinstance(node.op, ast.Or)
        and not contains_env_access(node)
        and any(is_default_literal(value) for value in node.values)
    )


def is_generic_default_if_expression(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.IfExp)
        and not contains_env_access(node)
        and (is_default_literal(node.body) or is_default_literal(node.orelse))
    )


def assigned_values_by_target(statements: list[ast.stmt]) -> dict[str, ast.AST]:
    values: dict[str, ast.AST] = {}
    for statement in statements:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                values[ast.dump(target)] = statement.value
        if isinstance(statement, ast.AnnAssign) and statement.value is not None:
            values[ast.dump(statement.target)] = statement.value
    return values


def has_same_target_fallback_assignment(
    primary_values: dict[str, ast.AST],
    fallback_values: dict[str, ast.AST],
    *,
    primary_predicate: Callable[[ast.AST], bool],
    fallback_predicate: Callable[[ast.AST], bool],
) -> bool:
    for target, primary_value in primary_values.items():
        fallback_value = fallback_values.get(target)
        if fallback_value is not None and primary_predicate(primary_value) and fallback_predicate(fallback_value):
            return True
    return False


def is_env_default_if_statement(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    body_values = assigned_values_by_target(node.body)
    else_values = assigned_values_by_target(node.orelse)
    return has_same_target_fallback_assignment(
        body_values,
        else_values,
        primary_predicate=contains_env_access,
        fallback_predicate=is_non_empty_default_value,
    ) or has_same_target_fallback_assignment(
        else_values,
        body_values,
        primary_predicate=contains_env_access,
        fallback_predicate=is_non_empty_default_value,
    )


def is_env_default_keyerror_handler(node: ast.AST) -> bool:
    if not isinstance(node, ast.Try):
        return False
    body_values = assigned_values_by_target(node.body)
    for handler in node.handlers:
        if not isinstance(handler.type, ast.Name) or handler.type.id != "KeyError":
            continue
        if has_same_target_fallback_assignment(
            body_values,
            assigned_values_by_target(handler.body),
            primary_predicate=contains_env_access,
            fallback_predicate=is_non_empty_default_value,
        ):
            return True
    return False


def is_argparse_default(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in ARGPARSE_CALL_NAMES:
        return False
    return any(keyword.arg == "default" for keyword in node.keywords)


def is_argparse_optional_positional(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr not in ARGPARSE_CALL_NAMES:
        return False
    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
        return False
    if node.args[0].value.startswith("-"):
        return False
    return any(
        keyword.arg == "nargs" and isinstance(keyword.value, ast.Constant) and keyword.value.value == "?"
        for keyword in node.keywords
    )


def args_attribute_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "args":
        return node.attr
    return None


def args_none_compare(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Compare):
        return None
    if len(node.ops) != 1 or not isinstance(node.ops[0], ast.Is):
        return None
    if len(node.comparators) != 1 or not isinstance(node.comparators[0], ast.Constant):
        return None
    if node.comparators[0].value is not None:
        return None
    return args_attribute_name(node.left)


def is_args_default_assignment(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    attr_name = args_none_compare(node.test)
    if attr_name is None:
        return False
    for statement in node.body:
        if not isinstance(statement, ast.Assign):
            continue
        for target in statement.targets:
            if args_attribute_name(target) == attr_name:
                return True
    return False


def is_args_default_if_expression(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.IfExp)
        and isinstance(node.test, ast.Name)
        and node.test.id == "args"
        and isinstance(node.body, ast.Subscript)
        and isinstance(node.body.value, ast.Name)
        and node.body.value.id == "args"
        and not is_absence_literal(node.orelse)
    )


def contains_raw_args_access(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Subscript) and isinstance(child.value, ast.Name) and child.value.id == "args"
        for child in ast.walk(node)
    )


def is_args_default_if_statement(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    body_values = assigned_values_by_target(node.body)
    else_values = assigned_values_by_target(node.orelse)
    return has_same_target_fallback_assignment(
        body_values,
        else_values,
        primary_predicate=contains_raw_args_access,
        fallback_predicate=lambda node: not is_absence_literal(node),
    ) or has_same_target_fallback_assignment(
        else_values,
        body_values,
        primary_predicate=contains_raw_args_access,
        fallback_predicate=lambda node: not is_absence_literal(node),
    )


def is_silent_default(node: ast.AST) -> bool:
    return (
        is_explicit_defaulting_call(node)
        or is_explicit_mapping_default_call(node)
        or is_argparse_default(node)
        or is_argparse_optional_positional(node)
        or is_env_or_default_expression(node)
        or is_env_default_if_expression(node)
        or is_generic_or_default_expression(node)
        or is_generic_default_if_expression(node)
        or is_env_default_if_statement(node)
        or is_env_default_keyerror_handler(node)
        or is_args_default_assignment(node)
        or is_args_default_if_expression(node)
        or is_args_default_if_statement(node)
    )


def function_default_nodes(node: ast.AST) -> list[ast.AST]:
    if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        return []

    return [
        default
        for default in [*node.args.defaults, *(default for default in node.args.kw_defaults if default is not None)]
        if not is_absence_literal(default)
    ]


def is_exception_fallback_handler(node: ast.AST) -> bool:
    if not isinstance(node, ast.ExceptHandler):
        return False

    for statement in node.body:
        if isinstance(statement, ast.Pass):
            return True
        if isinstance(statement, ast.Return) and statement.value is not None and not is_absence_literal(statement.value):
            return True

    return False


def source_snippet(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node)
    return f"<{type(node).__name__}>" if segment is None else " ".join(segment.split())


def _default_candidates(tree: ast.AST) -> Iterator[tuple[ast.AST, str]]:
    """Keep raw detection independent of reviewed contract applicability."""
    for node in ast.walk(tree):
        if is_silent_default(node):
            yield node, "python silent default"
        for default in function_default_nodes(node):
            yield default, "python function default"
        if is_exception_fallback_handler(node):
            yield node, "python exception fallback"


def _default_finding(path: Path, source: str, node: ast.AST, label: str) -> str:
    return f"{path.relative_to(ROOT)}:{node.lineno}: {label}: {source_snippet(source, node)}"


def python_default_findings(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    return [_default_finding(path, source, node, label) for node, label in _default_candidates(tree)]


def _contract_candidate(node: ast.AST, label: str) -> bool:
    # An annotation records a reviewed consumer contract, not inferred safety.
    # Environment, CLI, function-default and exception rules remain strict.
    if label != "python silent default" or contains_env_access(node) or contains_raw_args_access(node):
        return False
    if any(args_attribute_name(child) is not None for child in ast.walk(node)):
        return False
    return (
        is_explicit_mapping_default_call(node) or is_generic_or_default_expression(node) or is_generic_default_if_expression(node)
    )


def _contract_proof_exists(reference: str) -> bool:
    """Catch deleted or renamed proof symbols without importing their module.

    The caller admits a relative path and named class/method syntax first.
    Existence guards reference drift; a reviewer still owns its actual meaning.
    """
    filename, target = reference.split("::")
    class_name, method_name = target.split(".")
    path = ROOT
    for part in Path(filename).parts:
        path = path / part
        if path.is_symlink():
            return False
    if not path.is_file() or not method_name.startswith("test_"):
        return False
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return False
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
    return (
        len(classes) == 1
        and sum(isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == method_name for node in classes[0].body)
        == 1
    )


def python_operator_default_findings(path: Path) -> list[str]:
    """Apply one local reviewed exception without changing raw candidates.

    A standalone annotation covers one single-line generic candidate on the
    immediate statement line. A compound statement is allowed only for an If
    header; its body is never covered. The named proof must exist; its meaning
    remains review evidence, not a proof of arbitrary proof-state or consumer semantics.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    candidates = list(_default_candidates(tree))
    accepted: set[ast.AST] = set()
    findings: list[str] = []
    statements = [node for node in ast.walk(tree) if isinstance(node, ast.stmt)]
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type != tokenize.COMMENT or not token.string.startswith("# silent-defaults: allow-contract"):
            continue
        prefix, separator, detail = token.string.partition(" -- ")
        reason, proof_separator, proof = detail.rpartition("; proof=")
        next_line = token.end[0] + 1
        following = [node for node in statements if node.lineno == next_line]
        selected = [(node, label) for node, label in candidates if node.lineno == next_line]
        if (
            prefix == "# silent-defaults: allow-contract"
            and separator
            and reason.strip()
            and proof_separator
            and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9/]*\.py::[A-Za-z_]\w*\.[A-Za-z_]\w*", proof)
            and not token.line[: token.start[1]].strip()
            and len(following) == 1
            and (
                following[0].end_lineno == next_line
                or (isinstance(following[0], ast.If) and following[0].test.end_lineno == next_line)
            )
            and following[0].col_offset == token.start[1]
            and len(selected) == 1
            and selected[0][0].end_lineno == next_line
            and _contract_candidate(*selected[0])
            and _contract_proof_exists(proof)
        ):
            accepted.add(selected[0][0])
        else:
            findings.append(
                f"{path.relative_to(ROOT)}:{token.start[0]}: python contract annotation requires a reason, "
                "an existing proof=path.py::TestClass.test_method, and exactly one immediate generic candidate"
            )
    findings.extend(_default_finding(path, source, node, label) for node, label in candidates if node not in accepted)
    return findings


class PythonSilentDefaultsTest(unittest.TestCase):
    maxDiff = None

    def test_matchers_flag_representative_python_defaults(self) -> None:
        self.assertTrue(python_source_has_default('value = os.environ.get("PORT", "8080")'))
        self.assertTrue(python_source_has_default('value = config.get("ports", [])'))
        self.assertTrue(python_source_has_default('value = config.pop("profile", "demo")'))
        self.assertTrue(python_source_has_default('config.setdefault("profile", "demo")'))
        self.assertTrue(python_source_has_default('value = os.environ["PORT"].strip() or "8080"'))
        self.assertTrue(python_source_has_default("port = parsed.port or 443"))
        self.assertTrue(python_source_has_default('path = parsed.path or "/"'))
        self.assertTrue(python_source_has_default('manager = value if value is not None else "apk"'))
        self.assertTrue(python_source_has_default('value = env["PORT"] if "PORT" in env else "8080"'))
        self.assertTrue(python_source_has_default('if "PORT" in env:\n    port = env["PORT"]\nelse:\n    port = "8080"'))
        self.assertTrue(python_source_has_default('try:\n    port = env["PORT"]\nexcept KeyError:\n    port = "8080"'))
        self.assertTrue(python_source_has_default('parser.add_argument("--port", default="8080")'))
        self.assertTrue(python_source_has_default('parser.add_argument("profile", nargs="?")'))
        self.assertTrue(python_source_has_default('def run(port: str = "8080"):\n    pass'))
        self.assertTrue(python_source_has_default("try:\n    run()\nexcept Exception:\n    pass"))
        self.assertTrue(python_source_has_default("try:\n    run()\nexcept Exception:\n    return []"))
        self.assertTrue(python_source_has_default('if args.port is None:\n    args.port = "8080"'))
        self.assertTrue(python_source_has_default('profile = args[0] if args else "all"'))
        self.assertTrue(python_source_has_default('if args:\n    profile = args[0]\nelse:\n    profile = "all"'))

    def test_matchers_ignore_explicit_absence(self) -> None:
        self.assertFalse(python_source_has_default("value = default if raw is None else raw"))
        self.assertFalse(python_source_has_default('value = config.get("profile")'))
        self.assertFalse(python_source_has_default("def run(optional: str | None = None):\n    pass"))
        self.assertFalse(
            python_source_has_default('try:\n    run()\nexcept Exception as exc:\n    raise RuntimeError("failed") from exc')
        )
        self.assertFalse(python_source_has_default('optional = env["OPTIONAL"] if "OPTIONAL" in env else ""'))
        self.assertFalse(python_source_has_default('optional = env["OPTIONAL"] if "OPTIONAL" in env else None'))

    def test_python_file_scan_is_repository_wide(self) -> None:
        scanned = {path.relative_to(ROOT).as_posix() for path in python_files()}

        self.assertIn("tools/artifacts.py", scanned)
        self.assertIn("tools/bound_http_proxy.py", scanned)
        self.assertIn("repository_checks/test_conventions.py", scanned)

    def test_python_files_do_not_default_operator_inputs(self) -> None:
        findings: list[str] = []
        for path in python_files():
            findings.extend(python_operator_default_findings(path))

        self.assertEqual([], findings)


def python_source_has_default(source: str) -> bool:
    tree = ast.parse(source)
    return any(
        is_silent_default(node) or function_default_nodes(node) or is_exception_fallback_handler(node) for node in ast.walk(tree)
    )


if __name__ == "__main__":
    unittest.main()
