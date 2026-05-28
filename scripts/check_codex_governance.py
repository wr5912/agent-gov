#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

try:
    import yaml
except ImportError:  # pragma: no cover - 项目 requirements.txt 已包含 PyYAML。
    yaml = None


DEFAULT_SOURCE_ROOTS = ("app", "frontend/src")
PYTHON_SUFFIXES = {".py"}
FRONTEND_SUFFIXES = {".ts", ".tsx"}
EXCLUDED_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}


@dataclass(frozen=True)
class BudgetEntry:
    path: str
    max_lines: int
    target_lines: int | None = None
    due: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class SizeBudget:
    python_file_lines: int
    frontend_file_lines: int
    entries: dict[str, BudgetEntry]


@dataclass(frozen=True)
class GovernanceIssue:
    path: str
    lines: int
    limit: int
    message: str
    blocking: bool


def _load_yaml(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML is required. Run `uv pip install -r requirements.txt` first.")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data or {}


def load_size_budget(root: Path) -> SizeBudget:
    budget_path = root / ".codex" / "size-budget.yaml"
    if not budget_path.exists():
        return SizeBudget(python_file_lines=800, frontend_file_lines=800, entries={})

    data = _load_yaml(budget_path)
    limits = data.get("limits") or {}
    entries: dict[str, BudgetEntry] = {}
    for raw_entry in data.get("baseline") or []:
        rel_path = str(raw_entry["path"]).strip()
        max_lines = int(raw_entry["max_lines"])
        entries[rel_path] = BudgetEntry(
            path=rel_path,
            max_lines=max_lines,
            target_lines=(
                int(raw_entry["target_lines"])
                if raw_entry.get("target_lines") is not None
                else None
            ),
            due=str(raw_entry["due"]) if raw_entry.get("due") is not None else None,
            reason=str(raw_entry["reason"]) if raw_entry.get("reason") is not None else None,
        )

    return SizeBudget(
        python_file_lines=int(limits.get("python_file_lines", 800)),
        frontend_file_lines=int(limits.get("frontend_file_lines", 800)),
        entries=entries,
    )


def _is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_PARTS for part in path.parts)


def _relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_source_files(root: Path, source_roots: Iterable[str]) -> Iterable[Path]:
    for source_root in source_roots:
        base = root / source_root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or _is_excluded(path):
                continue
            if path.suffix in PYTHON_SUFFIXES or path.suffix in FRONTEND_SUFFIXES:
                yield path


def _count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8", errors="ignore") as handle:
        return sum(1 for _ in handle)


def _limit_for(path: Path, budget: SizeBudget) -> int | None:
    if path.suffix in PYTHON_SUFFIXES:
        return budget.python_file_lines
    if path.suffix in FRONTEND_SUFFIXES:
        return budget.frontend_file_lines
    return None


def collect_file_size_issues(
    root: Path,
    budget: SizeBudget,
    source_roots: Iterable[str] = DEFAULT_SOURCE_ROOTS,
) -> list[GovernanceIssue]:
    issues: list[GovernanceIssue] = []
    seen: set[str] = set()

    for path in _iter_source_files(root, source_roots):
        rel_path = _relative_path(root, path)
        seen.add(rel_path)
        limit = _limit_for(path, budget)
        if limit is None:
            continue

        lines = _count_lines(path)
        if lines <= limit:
            continue

        entry = budget.entries.get(rel_path)
        if entry is None:
            issues.append(
                GovernanceIssue(
                    path=rel_path,
                    lines=lines,
                    limit=limit,
                    message=f"unbudgeted oversized file: {lines} lines > {limit}",
                    blocking=True,
                )
            )
            continue

        if lines > entry.max_lines:
            issues.append(
                GovernanceIssue(
                    path=rel_path,
                    lines=lines,
                    limit=limit,
                    message=(
                        f"budgeted file grew: {lines} lines > budget max {entry.max_lines}; "
                        f"target is {entry.target_lines or limit}"
                    ),
                    blocking=True,
                )
            )
        else:
            issues.append(
                GovernanceIssue(
                    path=rel_path,
                    lines=lines,
                    limit=limit,
                    message=(
                        f"budgeted legacy debt: {lines} lines > {limit}; "
                        f"allowed max {entry.max_lines}, target {entry.target_lines or limit}"
                    ),
                    blocking=False,
                )
            )

    for rel_path, entry in sorted(budget.entries.items()):
        if rel_path not in seen and not (root / rel_path).exists():
            issues.append(
                GovernanceIssue(
                    path=rel_path,
                    lines=0,
                    limit=entry.target_lines or 0,
                    message="stale size-budget entry: file no longer exists",
                    blocking=False,
                )
            )

    return sorted(issues, key=lambda issue: issue.path)


def print_report(issues: list[GovernanceIssue], mode: str) -> None:
    if not issues:
        print("OK: no Codex governance size-budget issues found.")
        return

    for issue in issues:
        if issue.blocking:
            prefix = "FAIL" if mode == "fail" else "WARN"
        else:
            prefix = "BUDGET"
        print(f"{prefix}: {issue.path}: {issue.message}")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Codex architecture governance budgets.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root.")
    parser.add_argument("--mode", choices=("warn", "fail"), default="warn")
    parser.add_argument(
        "--source-root",
        action="append",
        dest="source_roots",
        help="Source root to scan. Defaults to app and frontend/src.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.root.resolve()
    source_roots = args.source_roots or list(DEFAULT_SOURCE_ROOTS)

    try:
        budget = load_size_budget(root)
        issues = collect_file_size_issues(root, budget, source_roots)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print_report(issues, args.mode)
    if args.mode == "fail" and any(issue.blocking for issue in issues):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
