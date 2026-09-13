"""正式验收源码扫描与仓库内依赖闭包解析。"""

from __future__ import annotations

import ast
import json
from pathlib import Path

from scripts.no_test_doubles_contract import (
    DOUBLE_CLASS_PREFIXES,
    DOUBLE_FUNCTION_PREFIXES,
    DYNAMIC_PYTHON_NAMES,
    FORBIDDEN_PYTHON_MODULE_PREFIXES,
    FORBIDDEN_PYTHON_NAMES,
    FORBIDDEN_TEST_HOOK_KEYWORDS,
    HTTP_ROUTE_DECORATORS,
    HTTP_ROUTE_REGISTRATION_METHODS,
    HTTP_ROUTE_TYPES,
    IGNORED_PARTS,
    IN_PROCESS_HTTP_CLIENT_IMPORTS,
    IN_PROCESS_HTTP_CLIENT_NAMES,
    JAVASCRIPT_LOCAL_IMPORT,
    JAVASCRIPT_RULES,
    JAVASCRIPT_SUFFIXES,
    LOCAL_SOURCE_SUFFIXES,
    PRODUCTION_COLLABORATOR_KEYWORDS,
    PROTECTED_MAKE_ASSIGNMENT,
    PYTHON_SUFFIXES,
    REPLACED_PRODUCTION_METHODS,
    REPO_ROOT,
    SHELL_SUFFIXES,
    TEST_ONLY_JAVASCRIPT_RULES,
    TRACE_FETCHER_SETTERS,
    Finding,
    FormalAcceptanceScope,
)


def _is_forbidden_python_module(module: str) -> bool:
    return any(module == prefix or module.startswith(f"{prefix}.") for prefix in FORBIDDEN_PYTHON_MODULE_PREFIXES)


class PythonDoubleVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, *, local_functions: frozenset[str] = frozenset()) -> None:
        self.path = path
        self.local_functions = local_functions
        self.dynamic_module_aliases = {"__builtins__"}
        self.findings: set[Finding] = set()

    def add(self, node: ast.AST, rule: str) -> None:
        self.findings.add(Finding(self.path.as_posix(), getattr(node, "lineno", 1), rule))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.split(".", maxsplit=1)[0] in {"builtins", "importlib", "runpy"}:
                self.dynamic_module_aliases.add(alias.asname or alias.name.split(".", maxsplit=1)[0])
            if _is_forbidden_python_module(alias.name):
                self.add(node, f"test-double module import {alias.name}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if _is_forbidden_python_module(node.module or "") or (node.module == "unittest" and any(alias.name == "mock" for alias in node.names)):
            imported = "unittest.mock" if node.module == "unittest" else node.module or "unittest.mock"
            self.add(node, f"test-double module import {imported}")
        forbidden_names = IN_PROCESS_HTTP_CLIENT_IMPORTS.get(node.module or "", frozenset())
        for alias in node.names:
            if alias.name in forbidden_names:
                self.add(node, f"in-process HTTP test client import {node.module}.{alias.name}")
            if node.module == "importlib" and alias.name in {"import_module", "reload"}:
                self.add(node, f"dynamic Python module loading {alias.name} is not auditable")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in DYNAMIC_PYTHON_NAMES:
            self.add(node, f"dynamic Python symbol {node.id} is not auditable")
        elif node.id in FORBIDDEN_PYTHON_NAMES:
            self.add(node, f"forbidden test-double symbol {node.id}")
        elif node.id in IN_PROCESS_HTTP_CLIENT_NAMES:
            self.add(node, f"in-process HTTP test client {node.id}")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in {
            "exec_module",
            "import_module",
            "load_module",
            "module_from_spec",
            "reload",
            "run_module",
            "run_path",
            "spec_from_file_location",
        }:
            self.add(node, f"dynamic Python module loading {node.attr} is not auditable")
        elif node.attr in FORBIDDEN_PYTHON_NAMES:
            self.add(node, f"forbidden test-double attribute {node.attr}")
        elif node.attr in IN_PROCESS_HTTP_CLIENT_NAMES:
            self.add(node, f"in-process HTTP test client {node.attr}")
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name.lstrip("_").startswith(DOUBLE_CLASS_PREFIXES):
            self.add(node, f"named test-double class {node.name}")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        normalized_name = node.name.lstrip("_")
        if normalized_name in {"fake", "mock", "stub"} or normalized_name.startswith(DOUBLE_FUNCTION_PREFIXES):
            self.add(node, f"named test-double function {node.name}")
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr in HTTP_ROUTE_DECORATORS:
                self.add(decorator, "acceptance-defined HTTP route replaces a production API boundary")
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if argument.arg == "monkeypatch":
                self.add(argument, "pytest monkeypatch fixture")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Name) and node.func.id in {"__import__", "compile", "eval", "exec"}:
            self.add(node, f"dynamic Python code execution {node.func.id} is not auditable")
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in self.dynamic_module_aliases
        ):
            self.add(node, "computed Python loader access is not auditable")
        if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == "httpx" and node.func.attr == "Response":
            self.add(node, "fabricated httpx.Response")
        if (isinstance(node.func, ast.Attribute) and node.func.attr in HTTP_ROUTE_REGISTRATION_METHODS) or (
            isinstance(node.func, ast.Name) and node.func.id in HTTP_ROUTE_TYPES
        ):
            self.add(node, "acceptance-defined HTTP route replaces a production API boundary")
        for keyword in node.keywords:
            if keyword.arg in FORBIDDEN_TEST_HOOK_KEYWORDS:
                self.add(keyword, f"test-only production hook {keyword.arg}")
            elif keyword.arg in PRODUCTION_COLLABORATOR_KEYWORDS and self._is_local_callable(keyword.value):
                self.add(keyword, f"injected production collaborator {keyword.arg}")
        if isinstance(node.func, ast.Attribute) and node.func.attr in TRACE_FETCHER_SETTERS:
            self.add(node, "replaced production trace provider")
        self._inspect_command_arguments(node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._inspect_assignment(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._inspect_assignment(node.target, node.value)
        self.generic_visit(node)

    def _inspect_assignment(self, target: ast.expr, value: ast.expr) -> None:
        if not isinstance(target, ast.Attribute):
            return
        if target.attr in REPLACED_PRODUCTION_METHODS:
            self.add(target, f"replaced production method {target.attr}")
        elif target.attr in PRODUCTION_COLLABORATOR_KEYWORDS and self._is_local_callable(value):
            self.add(target, f"injected production collaborator {target.attr}")

    def _inspect_command_arguments(self, node: ast.Call) -> None:
        values = (*node.args, *(keyword.value for keyword in node.keywords))
        for value in values:
            if not isinstance(value, (ast.List, ast.Tuple)):
                continue
            parts = [item.value for item in value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)]
            if any(PROTECTED_MAKE_ASSIGNMENT.fullmatch(part.strip()) for part in parts):
                self.add(value, "protected Make command assignment")
            if parts and parts[0].strip() in {":", "true"}:
                self.add(value, "no-op child command")
            if "--" in parts:
                child_index = parts.index("--") + 1
                if child_index < len(parts) and parts[child_index].strip() in {":", "true"}:
                    self.add(value, "no-op child command")

    def _is_local_callable(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Lambda) or (isinstance(node, ast.Name) and node.id in self.local_functions)


def scan_python(path: Path, *, repo_root: Path = REPO_ROOT) -> tuple[Finding, ...]:
    relative = path.relative_to(repo_root)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative.as_posix())
    except (OSError, SyntaxError) as exc:
        return (Finding(relative.as_posix(), getattr(exc, "lineno", 1) or 1, f"cannot inspect Python source: {exc}"),)
    local_functions = frozenset(node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    visitor = PythonDoubleVisitor(relative, local_functions=local_functions)
    visitor.visit(tree)
    return tuple(sorted(visitor.findings))


def scan_javascript(path: Path, *, repo_root: Path = REPO_ROOT) -> tuple[Finding, ...]:
    relative = path.relative_to(repo_root)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return (Finding(relative.as_posix(), 1, f"cannot inspect JavaScript source: {exc}"),)
    rules = JAVASCRIPT_RULES + TEST_ONLY_JAVASCRIPT_RULES
    findings = {
        Finding(relative.as_posix(), line_number, rule) for line_number, line in enumerate(lines, start=1) for pattern, rule in rules if pattern.search(line)
    }
    return tuple(sorted(findings))


def scan_shell(path: Path, *, repo_root: Path = REPO_ROOT) -> tuple[Finding, ...]:
    """Shell 可动态 source/eval；正式闭包一旦触达就 fail closed。"""

    relative = path.relative_to(repo_root)
    return (Finding(relative.as_posix(), 1, "formal shell source is not statically auditable"),)


def scan_paths(paths: tuple[Path, ...], *, repo_root: Path = REPO_ROOT) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    for path in paths:
        if path.suffix in PYTHON_SUFFIXES:
            findings.extend(scan_python(path, repo_root=repo_root))
        elif path.suffix in SHELL_SUFFIXES:
            findings.extend(scan_shell(path, repo_root=repo_root))
        else:
            findings.extend(scan_javascript(path, repo_root=repo_root))
    return tuple(sorted(findings))


def expand_local_import_closure(
    roots: tuple[Path, ...],
    *,
    repo_root: Path = REPO_ROOT,
) -> tuple[Path, ...]:
    """递归纳入正式验收资产实际导入的仓库内源码。"""

    resolved_root = repo_root.resolve()
    pending = list(roots)
    files: set[Path] = set()
    while pending:
        path = pending.pop().resolve()
        if path in files:
            continue
        try:
            path.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(f"formal live import escapes repository: {path}") from exc
        files.add(path)
        if path.suffix in PYTHON_SUFFIXES:
            dependencies = _python_local_imports(path, resolved_root)
        elif path.suffix in SHELL_SUFFIXES:
            dependencies = ()
        else:
            dependencies = _javascript_local_imports(path, resolved_root)
        pending.extend(dependency for dependency in dependencies if dependency not in files)
    return tuple(sorted(files))


def _python_local_imports(path: Path, repo_root: Path) -> tuple[Path, ...]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.relative_to(repo_root).as_posix())
    except (OSError, SyntaxError) as exc:
        raise ValueError(f"cannot inspect Python imports in {path}: {exc}") from exc
    dependencies: set[Path] = set()
    search_roots = _python_search_roots(path, repo_root)
    for node in ast.walk(tree):
        candidates: list[tuple[str, ...]] = []
        if isinstance(node, ast.Import):
            candidates.extend(tuple(alias.name.split(".")) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module_parts = tuple((node.module or "").split(".")) if node.module else ()
            if node.level:
                base = path.parent
                for _ in range(node.level - 1):
                    base = base.parent
                relative_parts = base.relative_to(repo_root).parts + module_parts
                candidates.append(relative_parts)
                candidates.extend(relative_parts + tuple(alias.name.split(".")) for alias in node.names if alias.name != "*")
            else:
                candidates.append(module_parts)
                candidates.extend(module_parts + tuple(alias.name.split(".")) for alias in node.names if alias.name != "*")
        for parts in candidates:
            resolved = _resolve_python_module(search_roots, parts)
            if resolved is not None:
                dependencies.add(resolved)
    return tuple(sorted(dependencies))


def _python_search_roots(importer: Path, repo_root: Path) -> tuple[Path, ...]:
    candidates = (
        importer.parent,
        repo_root,
        repo_root / "scripts",
        *(path for path in (repo_root / "packages").glob("*/src") if path.is_dir()),
    )
    return tuple(dict.fromkeys(path.resolve() for path in candidates if path.is_dir()))


def _resolve_python_module(search_roots: tuple[Path, ...], parts: tuple[str, ...]) -> Path | None:
    if not parts or any(not part or part == "__future__" for part in parts):
        return None
    candidates = tuple(candidate for root in search_roots for base in (root.joinpath(*parts),) for candidate in (base.with_suffix(".py"), base / "__init__.py"))
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)


def _javascript_local_imports(path: Path, repo_root: Path) -> tuple[Path, ...]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot inspect JavaScript imports in {path}: {exc}") from exc
    dependencies: set[Path] = set()
    for match in JAVASCRIPT_LOCAL_IMPORT.finditer(source):
        specifier = match.group(1)
        base = path.parent / specifier
        candidates = [base]
        if not base.suffix:
            candidates.extend(base.with_suffix(suffix) for suffix in sorted(JAVASCRIPT_SUFFIXES))
            candidates.extend(base / f"index{suffix}" for suffix in sorted(JAVASCRIPT_SUFFIXES))
        resolved = next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)
        if resolved is None:
            raise ValueError(f"formal live JavaScript import cannot be resolved: {path}: {specifier}")
        try:
            resolved.relative_to(repo_root)
        except ValueError as exc:
            raise ValueError(f"formal live JavaScript import escapes repository: {path}: {specifier}") from exc
        if resolved.suffix in LOCAL_SOURCE_SUFFIXES:
            dependencies.add(resolved)
    return tuple(sorted(dependencies))


def load_formal_acceptance_scope(
    policy_path: Path,
    *,
    repo_root: Path = REPO_ROOT,
) -> FormalAcceptanceScope:
    resolved_policy = policy_path if policy_path.is_absolute() else repo_root / policy_path
    try:
        payload = json.loads(resolved_policy.read_text(encoding="utf-8"))
        evidence = payload["test_evidence"]
        selectors = evidence["formal_live_selectors"]
        make_targets = evidence["formal_live_targets"]
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load formal acceptance scope from {resolved_policy}: {exc}") from exc
    if not isinstance(selectors, list) or not selectors or not all(isinstance(item, str) and item for item in selectors):
        raise ValueError("test_evidence.formal_live_selectors must be a non-empty string list")
    if not isinstance(make_targets, list) or not make_targets or not all(isinstance(item, str) and item for item in make_targets):
        raise ValueError("test_evidence.formal_live_targets must be a non-empty string list")

    files: set[Path] = set()
    for selector in selectors:
        selector_path = Path(selector)
        if selector_path.is_absolute() or ".." in selector_path.parts:
            raise ValueError(f"formal live selector must stay repository-relative: {selector}")
        matches = tuple(repo_root.glob(selector))
        if not matches:
            raise ValueError(f"formal live selector matches no assets: {selector}")
        for candidate in matches:
            if not candidate.is_file() or IGNORED_PARTS.intersection(candidate.parts):
                continue
            if candidate.suffix not in LOCAL_SOURCE_SUFFIXES:
                raise ValueError(f"formal live selector contains unsupported asset: {selector}: {candidate}")
            files.add(candidate.resolve())
    return FormalAcceptanceScope(
        files=tuple(sorted(files)),
        make_targets=tuple(dict.fromkeys(make_targets)),
    )


def validate_formal_target_allowlist(make_targets: tuple[str, ...]) -> None:
    """质量策略和公共 runner allowlist 必须双向完全一致。"""

    from scripts.container_acceptance_environment import acceptance_allowlisted_targets

    declared = frozenset(make_targets)
    expected = acceptance_allowlisted_targets()
    if declared == expected and len(make_targets) == len(declared):
        return
    missing = ", ".join(sorted(expected - declared)) or "none"
    unexpected = ", ".join(sorted(declared - expected)) or "none"
    duplicates = len(make_targets) - len(declared)
    raise ValueError(f"formal live targets must exactly match acceptance runner allowlist: missing={missing}; unexpected={unexpected}; duplicates={duplicates}")


def explicit_files(paths: tuple[Path, ...], *, repo_root: Path) -> tuple[Path, ...]:
    files: set[Path] = set()
    for raw_path in paths:
        absolute = raw_path if raw_path.is_absolute() else repo_root / raw_path
        candidates = (absolute,) if absolute.is_file() else absolute.rglob("*") if absolute.is_dir() else ()
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix in LOCAL_SOURCE_SUFFIXES:
                files.add(candidate.resolve())
    return tuple(sorted(files))
