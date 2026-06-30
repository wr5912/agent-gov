#!/usr/bin/env python3
"""Reject active stage-version labels outside archive history."""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

FORBIDDEN_TERMS = ("v" + "2.7", "v" + "27", "pre-" + "v" + "2.7")
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
        return f"FAIL: {self.path}{line}: active {self.location} uses stage-version label `{self.term}`"


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
