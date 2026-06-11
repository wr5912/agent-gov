#!/usr/bin/env python3
"""孤儿测试静态检测。

纯 AST 静态分析，找出 `tests/` 下对 `app`/`scripts` 中已删除模块或已删除符号的
import 引用，作为重构时「该删/该改哪些测试」的机械信号。不导入或执行被测代码，
因此比 pytest 收集更快、能一次性列出所有失效引用；它补充而非替代运行时测试。

保守策略（尽量零误报）：
- 只检查首方包 `app` / `scripts` 的绝对 import；相对 import、第三方、`tests` 内部
  helper 一律跳过。
- `from pkg import name`：name 命中「pkg 顶层定义/导入的符号」或「pkg 下存在同名
  子模块文件/包」即视为有效。
- 目标模块包含 `from x import *` 时无法判定其符号集合，跳过该模块的符号级检查。
- 只在目标模块文件确实不存在、或符号确实缺失时报告。
"""
from __future__ import annotations

import argparse
import ast
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

FIRST_PARTY_ROOTS = ("app", "scripts")
TESTS_DIR = "tests"
_BLOCK_BODY_ATTRS = ("body", "orelse", "finalbody")


@dataclass(frozen=True)
class OrphanIssue:
    path: str
    message: str


def _is_first_party(dotted: str) -> bool:
    return bool(dotted) and dotted.split(".")[0] in FIRST_PARTY_ROOTS


def _module_kind_and_file(root: Path, dotted: str) -> tuple[str, Path] | None:
    base = root.joinpath(*dotted.split("."))
    init = base / "__init__.py"
    if init.is_file():
        return ("package", init)
    module = base.with_suffix(".py")
    if module.is_file():
        return ("module", module)
    return None


def _submodule_exists(root: Path, package_dotted: str, name: str) -> bool:
    child = root.joinpath(*package_dotted.split("."), name)
    return child.with_suffix(".py").is_file() or (child / "__init__.py").is_file()


def _add_target_names(target: ast.expr, names: set[str]) -> None:
    if isinstance(target, ast.Name):
        names.add(target.id)
    elif isinstance(target, ast.Tuple | ast.List):
        for element in target.elts:
            _add_target_names(element, names)


def _collect_top_level_names(statements: Iterable[ast.stmt], names: set[str], star: list[bool]) -> None:
    for node in statements:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _add_target_names(target, names)
        elif isinstance(node, ast.AnnAssign):
            _add_target_names(node.target, names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "*":
                    star[0] = True
                else:
                    names.add(alias.asname or alias.name)
        elif isinstance(node, ast.If | ast.Try | ast.With | ast.AsyncWith):
            for attr in _BLOCK_BODY_ATTRS:
                _collect_top_level_names(getattr(node, attr, []), names, star)
            for handler in getattr(node, "handlers", []):
                _collect_top_level_names(handler.body, names, star)


def _defined_names(path: Path) -> set[str] | None:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return None
    names: set[str] = set()
    star = [False]
    _collect_top_level_names(tree.body, names, star)
    if star[0]:
        return None
    return names


def _iter_first_party_imports(tree: ast.AST) -> Iterable[tuple[str, tuple[str, ...]]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_first_party(alias.name):
                    yield (alias.name, ())
        elif isinstance(node, ast.ImportFrom):
            if node.level != 0 or node.module is None or not _is_first_party(node.module):
                continue
            names = tuple(alias.name for alias in node.names if alias.name != "*")
            yield (node.module, names)


def _import_issues(root: Path, test_rel: str) -> list[OrphanIssue]:
    try:
        tree = ast.parse((root / test_rel).read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return []
    issues: list[OrphanIssue] = []
    for module_dotted, names in _iter_first_party_imports(tree):
        resolved = _module_kind_and_file(root, module_dotted)
        if resolved is None:
            issues.append(OrphanIssue(test_rel, f"imports missing module `{module_dotted}`"))
            continue
        kind, module_file = resolved
        if not names:
            continue
        defined = _defined_names(module_file)
        if defined is None:
            continue
        for name in names:
            if name in defined:
                continue
            if kind == "package" and _submodule_exists(root, module_dotted, name):
                continue
            issues.append(OrphanIssue(test_rel, f"imports `{name}` not defined in `{module_dotted}`"))
    return issues


def _test_files(root: Path) -> list[str]:
    tests_dir = root / TESTS_DIR
    if not tests_dir.is_dir():
        return []
    return sorted(path.relative_to(root).as_posix() for path in tests_dir.rglob("test_*.py") if path.is_file())


def collect_orphan_issues(root: Path) -> list[OrphanIssue]:
    issues: list[OrphanIssue] = []
    for test_rel in _test_files(root):
        issues.extend(_import_issues(root, test_rel))
    return sorted(issues, key=lambda issue: (issue.path, issue.message))


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detect orphan tests referencing deleted app/scripts symbols.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.root.resolve()
    issues = collect_orphan_issues(root)
    if not issues:
        print("OK: no orphan tests found.")
        return 0
    for issue in issues:
        print(f"FAIL: {issue.path}: {issue.message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
