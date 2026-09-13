from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass

FORBIDDEN_MODULE_PREFIXES = (
    "agentgov_testkit._",
    "aioresponses",
    "httpretty",
    "pytest_httpserver",
    "responses",
    "respx",
    "unittest.mock",
    "vcr",
)
FORBIDDEN_SYMBOLS = frozenset(
    {
        "AsyncMock",
        "AgentInvocation",
        "AgentTestAgent",
        "MagicMock",
        "Mock",
        "MockTransport",
        "MonkeyPatch",
        "PropertyMock",
        "SimpleNamespace",
        "create_autospec",
        "mock_open",
        "monkeypatch",
        "patch",
    }
)
MUTATING_METHODS = frozenset(
    {
        "append",
        "clear",
        "extend",
        "insert",
        "pop",
        "popitem",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "update",
    }
)
SENSITIVE_ATTRIBUTES = frozenset({"errors", "invoke", "raw", "run", "text"})
DOUBLE_CLASS_PREFIXES = ("Fake", "Mock", "Stub")
DOUBLE_FUNCTION_PREFIXES = ("fake_", "mock_", "stub_")
FORBIDDEN_EARLY_EXIT_PATHS = frozenset(
    {
        ("builtins", "exit"),
        ("builtins", "quit"),
        ("os", "_exit"),
        ("os", "abort"),
        ("pytest", "importorskip"),
        ("pytest", "skip"),
        ("pytest", "xfail"),
        ("sys", "exit"),
        ("unittest", "skip"),
        ("unittest", "skipIf"),
        ("unittest", "skipUnless"),
    }
)
FORBIDDEN_PYTEST_MARKS = frozenset({"skip", "skipif", "xfail"})


@dataclass(frozen=True, order=True)
class SuiteIntegrityViolation:
    line: int
    message: str


def find_suite_integrity_violations(module: ast.Module) -> tuple[SuiteIntegrityViolation, ...]:
    result_names = _agent_result_names(module)
    visitor = _SuiteIntegrityVisitor(result_names, _import_aliases(module))
    visitor.visit(module)
    return tuple(sorted(visitor.violations))


def _import_aliases(module: ast.Module) -> Mapping[str, tuple[str, ...]]:
    aliases: dict[str, tuple[str, ...]] = {
        "exit": ("builtins", "exit"),
        "quit": ("builtins", "quit"),
    }
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound_name = alias.asname or alias.name.partition(".")[0]
                aliases[bound_name] = tuple(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_path = tuple(node.module.split("."))
            for alias in node.names:
                if alias.name != "*":
                    aliases[alias.asname or alias.name] = (*module_path, alias.name)
    return aliases


def _agent_result_names(module: ast.Module) -> frozenset[str]:
    result_names: set[str] = set()
    assignments: list[tuple[str, ast.expr]] = []
    for node in ast.walk(module):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            assignments.append((node.targets[0].id, node.value))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            assignments.append((node.target.id, node.value))
    changed = True
    while changed:
        changed = False
        for target, value in assignments:
            if target in result_names:
                continue
            if _is_agent_run_call(value) or (isinstance(value, ast.Name) and value.id in result_names):
                result_names.add(target)
                changed = True
    return frozenset(result_names)


def _is_agent_run_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "agent"
    )


def _is_forbidden_module(module: str) -> bool:
    return any(
        module.startswith(prefix) if prefix.endswith("_") else module == prefix or module.startswith(f"{prefix}.") for prefix in FORBIDDEN_MODULE_PREFIXES
    )


def _root_name(node: ast.expr) -> str | None:
    current = node
    while isinstance(current, (ast.Attribute, ast.Subscript)):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


def _attribute_path(node: ast.expr) -> tuple[str, ...]:
    parts: list[str] = []
    current = node
    while isinstance(current, (ast.Attribute, ast.Subscript)):
        if isinstance(current, ast.Attribute):
            parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return tuple(reversed(parts))


class _SuiteIntegrityVisitor(ast.NodeVisitor):
    def __init__(self, result_names: frozenset[str], import_aliases: Mapping[str, tuple[str, ...]]) -> None:
        self.result_names = result_names
        self.import_aliases = import_aliases
        self.violations: set[SuiteIntegrityViolation] = set()

    def _add(self, node: ast.AST, message: str) -> None:
        self.violations.add(SuiteIntegrityViolation(getattr(node, "lineno", 1), message))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _is_forbidden_module(alias.name):
                self._add(node, f"test-double module import is forbidden: {alias.name}")
            if alias.name.rpartition(".")[2] in FORBIDDEN_SYMBOLS:
                self._add(node, f"test-double symbol import is forbidden: {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if _is_forbidden_module(module) or (module == "unittest" and any(alias.name == "mock" for alias in node.names)):
            self._add(node, f"test-double module import is forbidden: {module or 'unittest.mock'}")
        for alias in node.names:
            if alias.name in FORBIDDEN_SYMBOLS:
                self._add(node, f"test-double symbol import is forbidden: {alias.name}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in FORBIDDEN_SYMBOLS:
            self._add(node, f"test-double symbol is forbidden: {node.id}")
        if self._normalized_path(node) in FORBIDDEN_EARLY_EXIT_PATHS:
            self._add(node, "skipping, xfail, or early process exit is forbidden")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in FORBIDDEN_SYMBOLS:
            self._add(node, f"test-double attribute is forbidden: {node.attr}")
        path = self._normalized_path(node)
        if path in FORBIDDEN_EARLY_EXIT_PATHS or (len(path) == 3 and path[:2] == ("pytest", "mark") and path[2] in FORBIDDEN_PYTEST_MARKS):
            self._add(node, "skipping, xfail, or early process exit is forbidden")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name.lstrip("_").startswith(DOUBLE_CLASS_PREFIXES):
            self._add(node, f"named test-double class is forbidden: {node.name}")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        normalized = node.name.lstrip("_")
        if normalized == "agent":
            self._add(node, "the platform agent fixture must not be defined or replaced")
        if normalized in {"fake", "mock", "stub"} or normalized.startswith(DOUBLE_FUNCTION_PREFIXES):
            self._add(node, f"named test-double function is forbidden: {node.name}")
        if normalized.startswith("pytest_"):
            self._add(node, f"Workspace-defined pytest hook is forbidden: {node.name}")
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if argument.arg == "monkeypatch":
                self._add(argument, "pytest monkeypatch fixture is forbidden")
        for decorator in node.decorator_list:
            if _is_autouse_fixture(decorator):
                self._add(decorator, "autouse pytest fixtures are forbidden")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._inspect_write(target, initial_agent_result=_is_agent_run_call(node.value))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._inspect_write(node.target, initial_agent_result=node.value is not None and _is_agent_run_call(node.value))
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._inspect_write(node.target)
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._inspect_write(node.target, initial_agent_result=_is_agent_run_call(node.value))
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for target in node.targets:
            self._inspect_write(target)
        self.generic_visit(node)

    def _inspect_write(self, target: ast.expr, *, initial_agent_result: bool = False) -> None:
        if isinstance(target, ast.Name):
            if target.id == "agent":
                self._add(target, "the platform agent fixture must not be reassigned")
            elif target.id in self.result_names and not initial_agent_result:
                self._add(target, "the real Agent invocation result must not be reassigned or aliased")
            elif target.id in {"pytest_plugins", "pytestmark"}:
                self._add(target, f"Workspace pytest control is forbidden: {target.id}")
            return
        root = _root_name(target)
        if root == "agent":
            self._add(target, "the platform agent fixture must not be mutated")
        elif root in self.result_names:
            self._add(target, "the real Agent invocation result must not be mutated")
        elif _attribute_path(target)[:2] in {("os", "environ"), ("sys", "modules"), ("sys", "path")}:
            self._add(target, "Python process execution controls must not be mutated")

    def visit_Call(self, node: ast.Call) -> None:
        path = self._normalized_path(node.func)
        if path in FORBIDDEN_EARLY_EXIT_PATHS or (len(path) == 3 and path[:2] == ("pytest", "mark") and path[2] in FORBIDDEN_PYTEST_MARKS):
            self._add(node, "skipping, xfail, or early process exit is forbidden")
        if isinstance(node.func, ast.Name) and node.func.id in {"__import__", "compile", "delattr", "eval", "exec", "setattr"}:
            self._add(node, f"dynamic code or attribute mutation is forbidden: {node.func.id}")
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"__delattr__", "__setattr__", "import_module", "reload"}:
            self._add(node, f"dynamic import or attribute mutation is forbidden: {node.func.attr}")
        if isinstance(node.func, ast.Attribute) and node.func.attr in MUTATING_METHODS:
            root = _root_name(node.func.value)
            if root == "agent":
                self._add(node, "the platform agent fixture must not be mutated")
            elif root in self.result_names:
                self._add(node, "the real Agent invocation result must not be mutated")
            elif _attribute_path(node.func.value)[:2] in {("os", "environ"), ("sys", "modules"), ("sys", "path")}:
                self._add(node, "Python process execution controls must not be mutated")
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"putenv", "main", "register"}:
            path = _attribute_path(node.func)
            if path[:1] in {("os",), ("pytest",)} or "pluginmanager" in path:
                self._add(node, "pytest or process execution controls must not be changed")
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        exception = node.exc
        if isinstance(exception, ast.Call):
            exception = exception.func
        if isinstance(exception, (ast.Name, ast.Attribute)) and self._normalized_path(exception) in {
            ("builtins", "SystemExit"),
            ("SystemExit",),
        }:
            self._add(node, "skipping, xfail, or early process exit is forbidden")
        self.generic_visit(node)

    def _normalized_path(self, node: ast.expr) -> tuple[str, ...]:
        path = _attribute_path(node)
        if not path:
            return ()
        prefix = self.import_aliases.get(path[0])
        return (*prefix, *path[1:]) if prefix else path


def _is_autouse_fixture(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if not (node.func.attr == "fixture" and isinstance(node.func.value, ast.Name) and node.func.value.id == "pytest"):
        return False
    return any(keyword.arg == "autouse" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True for keyword in node.keywords)
