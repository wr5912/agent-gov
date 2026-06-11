#!/usr/bin/env python3
"""docs/skill 容器治理硬门。

除 `docs/` 文档入口索引与归档索引外，也治理 `.codex/skills/` 与 `.claude/skills/`
下 Markdown 的未完成标记和镜像同步（见 TEXT_GOVERNANCE_ROOTS / MIRRORED_SKILLS）；
产品内容是否正确仍由 agentgov-governance-preflight 负责，本脚本只做容器治理。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

DOCS_INDEX = "docs/README.md"
ARCHIVE_INDEX = "docs/archive/README.md"
ARCHIVE_INDEX_HEADERS = ("原路径", "归档路径", "替代文档", "归档日期")
TEXT_GOVERNANCE_ROOTS = ("docs/", ".codex/skills/", ".claude/skills/")
# CJK 标记按子串匹配；ASCII 标记按整词匹配，避免 `test_xxx.py`、掩码值等误报。
_CJK_UNFINISHED_MARKERS = ("待" "补充", "占" "位")
_WORD_UNFINISHED_MARKERS = ("TO" "DO", "TB" "D", "place" "holder", "x" "xx", "X" "XX")
_WORD_UNFINISHED_PATTERN = re.compile(r"\b(?:" + "|".join(_WORD_UNFINISHED_MARKERS) + r")\b")
MIRRORED_SKILLS = (
    (".codex/skills/docs-governance/SKILL.md", ".claude/skills/docs-governance/SKILL.md"),
    (".codex/skills/runtime-env-governance/SKILL.md", ".claude/skills/runtime-env-governance/SKILL.md"),
    (".codex/skills/agentgov-governance-preflight/SKILL.md", ".claude/skills/agentgov-governance-preflight/SKILL.md"),
)


@dataclass(frozen=True)
class DocsGovernanceIssue:
    path: str
    message: str
    blocking: bool = True


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(root), *args], check=False, capture_output=True, text=True)


def _git_show(root: Path, ref: str, rel_path: str) -> str | None:
    result = _git(root, ["show", f"{ref}:{rel_path}"])
    if result.returncode != 0:
        return None
    return result.stdout


def _repo_paths(root: Path) -> set[str]:
    result = _git(root, ["ls-files", "--cached", "--others", "--exclude-standard"])
    if result.returncode == 0:
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and ".git" not in path.parts}


def _is_new_path(root: Path, base_ref: str | None, rel_path: str) -> bool:
    if base_ref is None:
        return True
    return _git_show(root, base_ref, rel_path) is None


def _is_changed_path(root: Path, base_ref: str | None, rel_path: str) -> bool:
    if base_ref is None:
        return True
    base_text = _git_show(root, base_ref, rel_path)
    if base_text is None:
        return True
    return _read_existing(root, rel_path) != base_text


def _is_markdown_doc(rel_path: str) -> bool:
    return rel_path.startswith("docs/") and Path(rel_path).suffix == ".md"


def _is_active_doc(rel_path: str) -> bool:
    return _is_markdown_doc(rel_path) and rel_path != DOCS_INDEX and not rel_path.startswith("docs/archive/")


def _is_archive_doc(rel_path: str) -> bool:
    return _is_markdown_doc(rel_path) and rel_path.startswith("docs/archive/") and rel_path != ARCHIVE_INDEX


def _read_existing(root: Path, rel_path: str) -> str:
    path = root / rel_path
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _new_paths(root: Path, base_ref: str | None, paths: Iterable[str]) -> list[str]:
    return sorted(path for path in paths if _is_new_path(root, base_ref, path))


def _active_doc_index_issues(root: Path, new_active_docs: list[str]) -> list[DocsGovernanceIssue]:
    if not new_active_docs:
        return []
    index = _read_existing(root, DOCS_INDEX)
    if not index:
        return [DocsGovernanceIssue(DOCS_INDEX, "docs index is required when adding active docs")]
    return [
        DocsGovernanceIssue(path, f"new active docs file is not linked from {DOCS_INDEX}")
        for path in new_active_docs
        if path not in index
    ]


def _archive_index_issues(root: Path, new_archive_docs: list[str]) -> list[DocsGovernanceIssue]:
    if not new_archive_docs:
        return []
    index = _read_existing(root, ARCHIVE_INDEX)
    if not index:
        return [DocsGovernanceIssue(ARCHIVE_INDEX, "archive index is required when adding archived docs")]
    issues = [
        DocsGovernanceIssue(ARCHIVE_INDEX, f"archive index is missing required column: {header}")
        for header in ARCHIVE_INDEX_HEADERS
        if header not in index
    ]
    issues.extend(
        DocsGovernanceIssue(path, f"new archived docs file is not listed in {ARCHIVE_INDEX}") for path in new_archive_docs if path not in index
    )
    return issues


def _normalized_skill_text(text: str) -> str:
    lines: list[str] = []
    skip_next_blank = False
    for line in text.splitlines():
        if line.startswith("> 本技能与 `"):
            skip_next_blank = True
            continue
        if skip_next_blank and not line.strip():
            skip_next_blank = False
            continue
        skip_next_blank = False
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _skill_mirror_issues(root: Path) -> list[DocsGovernanceIssue]:
    issues: list[DocsGovernanceIssue] = []
    for codex_path, claude_path in MIRRORED_SKILLS:
        codex_text = _read_existing(root, codex_path)
        claude_text = _read_existing(root, claude_path)
        if bool(codex_text) != bool(claude_text):
            issues.append(DocsGovernanceIssue(codex_path, f"mirrored skill pair is incomplete: {claude_path}"))
            continue
        if codex_text and _normalized_skill_text(codex_text) != _normalized_skill_text(claude_text):
            issues.append(DocsGovernanceIssue(codex_path, f"mirrored skill differs from {claude_path}"))
    return issues


def _text_governed_path(rel_path: str) -> bool:
    return rel_path.endswith(".md") and rel_path.startswith(TEXT_GOVERNANCE_ROOTS)


def _find_unfinished_marker(line: str) -> str | None:
    for marker in _CJK_UNFINISHED_MARKERS:
        if marker in line:
            return marker
    match = _WORD_UNFINISHED_PATTERN.search(line)
    return match.group(0) if match else None


def _unfinished_marker_issues(root: Path, base_ref: str | None, paths: Iterable[str]) -> list[DocsGovernanceIssue]:
    issues: list[DocsGovernanceIssue] = []
    changed_paths = sorted(path for path in paths if _text_governed_path(path) and _is_changed_path(root, base_ref, path))
    for rel_path in changed_paths:
        for line_number, line in enumerate(_read_existing(root, rel_path).splitlines(), start=1):
            marker = _find_unfinished_marker(line)
            if marker is not None:
                issues.append(DocsGovernanceIssue(rel_path, f"unfinished marker `{marker}` at line {line_number}"))
    return issues


def collect_docs_governance_issues(root: Path, base_ref: str | None) -> list[DocsGovernanceIssue]:
    paths = _repo_paths(root)
    new_paths = _new_paths(root, base_ref, paths)
    new_active_docs = [path for path in new_paths if _is_active_doc(path)]
    new_archive_docs = [path for path in new_paths if _is_archive_doc(path)]
    issues: list[DocsGovernanceIssue] = []
    issues.extend(_active_doc_index_issues(root, new_active_docs))
    issues.extend(_archive_index_issues(root, new_archive_docs))
    issues.extend(_skill_mirror_issues(root))
    issues.extend(_unfinished_marker_issues(root, base_ref, paths))
    return sorted(issues, key=lambda issue: (issue.path, issue.message))


def _resolve_base_ref(root: Path, requested: str | None) -> str | None:
    if requested:
        return requested
    result = _git(root, ["rev-parse", "--verify", "HEAD"])
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check docs governance rules.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root.")
    parser.add_argument("--base-ref", help="Git ref to compare against. Defaults to HEAD.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    root = args.root.resolve()
    base_ref = _resolve_base_ref(root, args.base_ref)
    issues = collect_docs_governance_issues(root, base_ref)
    if not issues:
        print("OK: no docs governance issues found.")
        return 0
    for issue in issues:
        print(f"FAIL: {issue.path}: {issue.message}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
