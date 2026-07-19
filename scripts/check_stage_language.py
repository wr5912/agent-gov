#!/usr/bin/env python3
"""Reject retired stage labels and user-facing governance terminology."""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_TERMS = ("v" + "2.7", "v" + "27", "pre-" + "v" + "2.7")
DISPLAY_FORBIDDEN_TERMS = (
    "基线版本",
    "候选版本",
    "变更集",
    "发布门禁",
    "测试数据集",
    "change set",
)
FRONTEND_DISPLAY_PREFIX = "frontend/src/"
FRONTEND_DISPLAY_SUFFIXES = (".ts", ".tsx")
FRONTEND_DISPLAY_SKIP = {"frontend/src/types/api.ts"}
AUTHORITY_DOC_MARKERS: dict[str, str | None] = {
    "docs/AgentGov_四阶段改进治理工作台UI整改方案.md": None,
    "docs/AgentGov术语与版本边界.md": "## 5. 迁移前历史名称映射",
    "docs/engineering/业务AgentWorkspace原生pytest测试资产实现方案.md": None,
}
OPENAPI_TEXT_KEYWORDS = {"summary", "description"}
SKIP_PREFIXES = (
    "docs/archive/",
    ".claude/worktrees/",
    "frontend/node_modules/",
    "frontend/dist/",
)
SKIP_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


@dataclass(frozen=True)
class StageLanguageIssue:
    path: str
    line: int | None
    term: str
    location: str

    def format(self) -> str:
        line = "" if self.line is None else f":{self.line}"
        return f"FAIL: {self.path}{line}: active {self.location} uses retired label `{self.term}`"


def _git_paths(root: Path) -> list[str] | None:
    result = subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotePath=false", "ls-files", "--cached", "--others", "--exclude-standard"],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="ignore",
        text=True,
    )
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _fallback_paths(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


def _repo_paths(root: Path) -> list[str]:
    return _git_paths(root) or _fallback_paths(root)


def _should_skip(rel_path: str) -> bool:
    if any(rel_path.startswith(prefix) for prefix in SKIP_PREFIXES):
        return True
    return any(part in SKIP_PARTS for part in Path(rel_path).parts)


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="ignore")


def _matching_terms(value: str) -> list[str]:
    lower = value.lower()
    return [term for term in FORBIDDEN_TERMS if term in lower]


def _matching_display_terms(value: str) -> list[str]:
    lower = value.lower()
    return [term for term in DISPLAY_FORBIDDEN_TERMS if term.lower() in lower]


def _append_content_issues(
    issues: list[StageLanguageIssue],
    *,
    rel_path: str,
    text: str,
    location: str,
) -> None:
    for line_no, line in enumerate(text.splitlines(), start=1):
        for term in _matching_display_terms(line):
            issues.append(StageLanguageIssue(rel_path, line_no, term, location))


def _literal_string(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError):
        return None
    return value if isinstance(value, str) else None


def _collect_openapi_issues(rel_path: str, text: str) -> list[StageLanguageIssue]:
    if not rel_path.startswith("app/") or not rel_path.endswith(".py"):
        return []
    try:
        tree = ast.parse(text, filename=rel_path)
    except SyntaxError:
        return []

    issues: list[StageLanguageIssue] = []
    for node in ast.walk(tree):
        values: list[tuple[str, int]] = []
        if isinstance(node, ast.Call):
            call_name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
            for keyword in node.keywords:
                is_route_summary = keyword.arg == "summary" and call_name in {
                    "delete",
                    "get",
                    "head",
                    "options",
                    "patch",
                    "post",
                    "put",
                    "trace",
                }
                is_schema_description = keyword.arg == "description" and call_name in {
                    "Body",
                    "Cookie",
                    "File",
                    "Form",
                    "Header",
                    "Path",
                    "Query",
                    "Field",
                }
                if is_route_summary or is_schema_description:
                    value = _literal_string(keyword.value)
                    if value is not None:
                        values.append((value, getattr(keyword.value, "lineno", node.lineno)))
        elif isinstance(node, ast.ClassDef) and rel_path.endswith("_schemas.py"):
            value = ast.get_docstring(node, clean=False)
            if value is not None:
                values.append((value, node.lineno))

        for value, line_no in values:
            for term in _matching_display_terms(value):
                issues.append(StageLanguageIssue(rel_path, line_no, term, "OpenAPI text"))
    return issues


def collect_issues(root: Path) -> list[StageLanguageIssue]:
    issues: list[StageLanguageIssue] = []
    for rel_path in _repo_paths(root):
        if _should_skip(rel_path):
            continue
        if not (root / rel_path).exists():
            continue

        for term in _matching_terms(rel_path):
            issues.append(StageLanguageIssue(rel_path, None, term, "path"))

        text = _read_text(root / rel_path)
        if text is None:
            continue
        for line_no, line in enumerate(text.splitlines(), start=1):
            for term in _matching_terms(line):
                issues.append(StageLanguageIssue(rel_path, line_no, term, "content"))

        if rel_path.startswith(FRONTEND_DISPLAY_PREFIX) and rel_path.endswith(FRONTEND_DISPLAY_SUFFIXES) and rel_path not in FRONTEND_DISPLAY_SKIP:
            _append_content_issues(
                issues,
                rel_path=rel_path,
                text=text,
                location="user-facing UI text",
            )

        if rel_path in AUTHORITY_DOC_MARKERS:
            marker = AUTHORITY_DOC_MARKERS[rel_path]
            active_text = text.split(marker, 1)[0] if marker and marker in text else text
            _append_content_issues(
                issues,
                rel_path=rel_path,
                text=active_text,
                location="authority document text",
            )

        issues.extend(_collect_openapi_issues(rel_path, text))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=Path.cwd(), type=Path, help="repository root")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    issues = collect_issues(root)
    if issues:
        for issue in issues:
            print(issue.format())
        return 1
    print("stage language OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
