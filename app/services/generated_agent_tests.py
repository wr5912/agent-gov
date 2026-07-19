from __future__ import annotations

import ast
import hashlib
import re
import sys
from dataclasses import dataclass

from app.runtime.errors import BusinessRuleViolation

MAX_GENERATED_TEST_BYTES = 64 * 1024
MAX_GENERATED_TEST_LINES = 60
_ALLOWED_THIRD_PARTY_IMPORTS = {"agentgov_testkit", "pytest"}
_DISALLOWED_PYTEST_CALLS = {"skip", "skipif", "xfail"}


class GeneratedAgentTestError(BusinessRuleViolation):
    pass


@dataclass(frozen=True)
class GeneratedAgentTestCandidate:
    target_path: str
    test_code: str
    test_intent: str
    assertion_rationale: str

    def to_payload(self) -> dict[str, str]:
        return {
            "target_path": self.target_path,
            "test_code": self.test_code,
            "test_intent": self.test_intent,
            "assertion_rationale": self.assertion_rationale,
        }


def build_generated_agent_test(
    *,
    improvement_id: str,
    index: int,
    test_code: str,
    test_intent: str,
    assertion_rationale: str,
) -> GeneratedAgentTestCandidate:
    code = normalize_generated_test_code(test_code)
    validate_generated_test_code(code)
    intent = test_intent.strip()
    rationale = assertion_rationale.strip()
    if not intent:
        raise GeneratedAgentTestError("generated pytest requires a non-empty test_intent")
    if not rationale:
        raise GeneratedAgentTestError("generated pytest requires a non-empty assertion_rationale")
    safe_id = re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_]", "_", improvement_id)).strip("_")[:48] or "improvement"
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()[:12]
    target_path = f"tests/test_feedback_{safe_id}_{index:02d}_{digest}.py"
    return GeneratedAgentTestCandidate(
        target_path=target_path,
        test_code=code,
        test_intent=intent,
        assertion_rationale=rationale,
    )


def normalize_generated_test_code(source: str) -> str:
    code = source.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not code:
        raise GeneratedAgentTestError("generated pytest code must not be empty")
    if "```" in code:
        raise GeneratedAgentTestError("generated pytest code must not contain Markdown fences")
    if len(code.encode("utf-8")) > MAX_GENERATED_TEST_BYTES:
        raise GeneratedAgentTestError(f"generated pytest code exceeds {MAX_GENERATED_TEST_BYTES} bytes")
    return f"{code}\n"


def validate_generated_test_code(source: str) -> None:
    if len(source.splitlines()) > MAX_GENERATED_TEST_LINES:
        raise GeneratedAgentTestError(f"generated pytest code exceeds {MAX_GENERATED_TEST_LINES} lines")
    try:
        module = ast.parse(source, filename="<generated-agent-test>")
    except SyntaxError as exc:
        raise GeneratedAgentTestError(f"generated pytest code is not parseable: {exc}") from exc

    _validate_imports(module)
    _reject_bypass_constructs(module)
    test_functions = [node for node in module.body if isinstance(node, ast.FunctionDef) and node.name.startswith("test_")]
    if not test_functions:
        raise GeneratedAgentTestError("generated pytest code must define at least one top-level test_* function")
    if len(test_functions) > 1:
        raise GeneratedAgentTestError("generated pytest code must define exactly one top-level test_* function")
    helper_functions = [node for node in module.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node not in test_functions]
    if helper_functions:
        raise GeneratedAgentTestError("generated pytest code must not define helper functions")
    if any(isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("test_") for node in module.body):
        raise GeneratedAgentTestError("generated pytest code must use synchronous test functions")
    for function in test_functions:
        _validate_test_function(function)


def _validate_imports(module: ast.Module) -> None:
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
            if any(alias.asname == "agent" for alias in node.names):
                raise GeneratedAgentTestError("generated pytest code must use the injected agent fixture, not import agent")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                raise GeneratedAgentTestError("generated pytest code must not use relative imports")
            modules = [node.module or ""]
            if (node.module or "").partition(".")[0] == "agentgov_testkit" and any(
                alias.name in {"agent", "*"} or alias.asname == "agent" for alias in node.names
            ):
                raise GeneratedAgentTestError("generated pytest code must use the injected agent fixture, not import agent")
        else:
            continue
        for module_name in modules:
            root = module_name.partition(".")[0]
            if root not in sys.stdlib_module_names and root not in _ALLOWED_THIRD_PARTY_IMPORTS:
                raise GeneratedAgentTestError(f"generated pytest imports unsupported module: {root or module_name}")


def _reject_bypass_constructs(module: ast.Module) -> None:
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == "agent":
                raise GeneratedAgentTestError("generated pytest code must not define or override the agent fixture")
            for decorator in node.decorator_list:
                if _is_pytest_member(decorator, "fixture"):
                    raise GeneratedAgentTestError("generated pytest code must not define pytest fixtures")
                if _is_disallowed_pytest_call(decorator):
                    raise GeneratedAgentTestError("generated pytest code must not skip or xfail tests")
        if isinstance(node, ast.Call) and _is_disallowed_pytest_call(node.func):
            raise GeneratedAgentTestError("generated pytest code must not skip or xfail tests")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "agent" and node.attr == "invoke":
                raise GeneratedAgentTestError("generated pytest code must call agent.run(), not agent.invoke()")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "pytestmark" for target in targets):
                raise GeneratedAgentTestError("generated pytest code must not define pytestmark")


def _validate_test_function(function: ast.FunctionDef) -> None:
    parameter_names = {argument.arg for argument in [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]}
    if "agent" not in parameter_names:
        raise GeneratedAgentTestError(f"{function.name} must declare the agent fixture")

    result_names: set[str] = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Call) and _is_agent_run_call(node):
            parent_assignment = _assignment_for_call(function, node)
            if parent_assignment is None:
                raise GeneratedAgentTestError(f"{function.name} must assign agent.run() to a result variable")
            result_names.add(parent_assignment)
    if not result_names:
        raise GeneratedAgentTestError(f"{function.name} must call agent.run()")

    assertions = [node for node in function.body if isinstance(node, ast.Assert)]
    if len(assertions) != sum(isinstance(node, ast.Assert) for node in ast.walk(function)):
        raise GeneratedAgentTestError(f"{function.name} must keep assertions directly in the test body")
    if not assertions or not _is_success_assertion(assertions[0].test, result_names):
        raise GeneratedAgentTestError(f"{function.name} must assert that result.errors is empty")
    output_aliases = _collect_canonical_output_aliases(function, result_names)
    business_assertions = assertions[1:]
    if not business_assertions or any(not _is_business_result_assertion(node.test, result_names, output_aliases) for node in business_assertions):
        raise GeneratedAgentTestError(f"{function.name} must assert every concrete business outcome using canonical normalized text or result.raw")


def _assignment_for_call(function: ast.FunctionDef, call: ast.Call) -> str | None:
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and node.value is call and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
        if isinstance(node, ast.AnnAssign) and node.value is call and isinstance(node.target, ast.Name):
            return node.target.id
    return None


def _is_agent_run_call(call: ast.Call) -> bool:
    function = call.func
    return isinstance(function, ast.Attribute) and function.attr == "run" and isinstance(function.value, ast.Name) and function.value.id == "agent"


def _collect_canonical_output_aliases(function: ast.FunctionDef, result_names: set[str]) -> set[str]:
    aliases: set[str] = set()
    for statement in function.body:
        if isinstance(statement, ast.Assign):
            targets = [target.id for target in statement.targets if isinstance(target, ast.Name)]
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value is not None:
            targets = [statement.target.id]
            value = statement.value
        else:
            continue
        if _is_canonical_text_normalization(value, result_names) or _references_raw_output(value, result_names, aliases):
            aliases.update(targets)
        else:
            aliases.difference_update(targets)
    return aliases


def _is_canonical_text_normalization(expression: ast.expr, result_names: set[str]) -> bool:
    if not isinstance(expression, ast.Call) or expression.keywords or len(expression.args) != 1:
        return False
    join = expression.func
    if not (isinstance(join, ast.Attribute) and join.attr == "join" and isinstance(join.value, ast.Constant) and join.value.value == ""):
        return False
    split_call = expression.args[0]
    if not isinstance(split_call, ast.Call) or split_call.args or split_call.keywords:
        return False
    split = split_call.func
    return (
        isinstance(split, ast.Attribute)
        and split.attr == "split"
        and isinstance(split.value, ast.Attribute)
        and split.value.attr == "text"
        and isinstance(split.value.value, ast.Name)
        and split.value.value.id in result_names
    )


def _references_raw_output(expression: ast.expr, result_names: set[str], aliases: set[str]) -> bool:
    for node in ast.walk(expression):
        if isinstance(node, ast.Name) and node.id in aliases:
            return True
        if not isinstance(node, ast.Attribute) or node.attr != "raw":
            continue
        if isinstance(node.value, ast.Name) and node.value.id in result_names:
            return True
    return False


def _is_business_result_assertion(
    expression: ast.expr,
    result_names: set[str],
    output_aliases: set[str],
) -> bool:
    if _is_no_tool_activity_assertion(expression, result_names):
        return True
    if not _references_business_output(expression, result_names, output_aliases):
        return False
    if _contains_weak_output_check(expression):
        return False
    if isinstance(expression, ast.Compare):
        return _contains_meaningful_literal(expression)
    if isinstance(expression, ast.BoolOp):
        if isinstance(expression.op, ast.Or):
            return False
        return all(_is_business_result_assertion(value, result_names, output_aliases) for value in expression.values)
    if isinstance(expression, ast.UnaryOp):
        return _is_business_result_assertion(expression.operand, result_names, output_aliases)
    if isinstance(expression, ast.Call):
        return _contains_meaningful_literal(expression)
    return False


def _is_no_tool_activity_assertion(expression: ast.expr, result_names: set[str]) -> bool:
    if not (
        isinstance(expression, ast.Compare)
        and len(expression.ops) == 1
        and isinstance(expression.ops[0], ast.Eq)
        and len(expression.comparators) == 1
        and isinstance(expression.comparators[0], ast.List)
        and not expression.comparators[0].elts
    ):
        return False
    path: list[str] = []
    current = expression.left
    while isinstance(current, ast.Subscript) and isinstance(current.slice, ast.Constant) and isinstance(current.slice.value, str):
        path.append(current.slice.value)
        current = current.value
    path.reverse()
    return (
        path == ["agent_activity", "tool_calls"]
        and isinstance(current, ast.Attribute)
        and current.attr == "raw"
        and isinstance(current.value, ast.Name)
        and current.value.id in result_names
    )


def _contains_weak_output_check(expression: ast.expr) -> bool:
    for node in ast.walk(expression):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in {"any", "all", "bool", "len"}:
            return True
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"strip", "get"}:
            return True
    return False


def _contains_meaningful_literal(expression: ast.expr) -> bool:
    for node in ast.walk(expression):
        if not isinstance(node, ast.Constant):
            continue
        value = node.value
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str):
            if value.strip():
                return True
            continue
        if isinstance(value, (int, float, complex)):
            return True
    return False


def _is_success_assertion(expression: ast.expr, result_names: set[str]) -> bool:
    if isinstance(expression, ast.UnaryOp) and isinstance(expression.op, ast.Not):
        return _is_result_errors_reference(expression.operand, result_names)
    return False


def _is_result_errors_reference(expression: ast.expr, result_names: set[str]) -> bool:
    return (
        isinstance(expression, ast.Attribute) and expression.attr == "errors" and isinstance(expression.value, ast.Name) and expression.value.id in result_names
    )


def _references_business_output(
    expression: ast.expr,
    result_names: set[str],
    output_aliases: set[str] | None = None,
) -> bool:
    aliases = output_aliases or set()
    for node in ast.walk(expression):
        if isinstance(node, ast.Name) and node.id in aliases:
            return True
        if not isinstance(node, ast.Attribute) or node.attr != "raw":
            continue
        if isinstance(node.value, ast.Name) and node.value.id in result_names:
            return True
    return False


def _is_disallowed_pytest_call(node: ast.expr) -> bool:
    if isinstance(node, ast.Call):
        return _is_disallowed_pytest_call(node.func)
    if not isinstance(node, ast.Attribute) or node.attr not in _DISALLOWED_PYTEST_CALLS:
        return False
    if isinstance(node.value, ast.Name):
        return node.value.id == "pytest"
    return isinstance(node.value, ast.Attribute) and node.value.attr == "mark" and isinstance(node.value.value, ast.Name) and node.value.value.id == "pytest"


def _is_pytest_member(node: ast.expr, member: str) -> bool:
    if isinstance(node, ast.Call):
        return _is_pytest_member(node.func, member)
    return isinstance(node, ast.Attribute) and node.attr == member and isinstance(node.value, ast.Name) and node.value.id == "pytest"
