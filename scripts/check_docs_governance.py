#!/usr/bin/env python3
"""docs/skill 容器治理硬门。

除 `docs/` 文档入口索引与归档索引外，也治理 `.codex/skills/` 与 `.claude/skills/`
下 Markdown 的未完成标记和镜像同步（见 TEXT_GOVERNANCE_ROOTS / collect_mirrored_skill_pairs）；
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
LOCAL_ARTIFACT_PATH_MARKERS = ("/mnt/data/", "ghostwriter_images")
CODEX_SKILLS_ROOT = ".codex/skills"
CLAUDE_SKILLS_ROOT = ".claude/skills"
LONG_TERM_AUTHORITY_DOCS = frozenset(
    {
        "docs/项目目标愿景使命.md",
        "docs/AgentGov核心功能测试用例.md",
    }
)
LEGACY_GOVERNANCE_AGENT_TERMS = (
    "attribution-analyzer",
    "proposal-generator",
    "execution-optimizer",
    "eval-case-governor",
    "regression-impact-analyzer",
)
MIRRORED_SKILL_EXCLUSIONS = frozenset(
    {
        "project-skill",  # 通用模板，两侧按各自工具形态维护。
        "codex-config-optimizer",  # Codex-only 配置治理工具。
    }
)
# CJK 标记按子串匹配；ASCII 标记按整词匹配，避免 `test_xxx.py`、掩码值等误报。
_CJK_UNFINISHED_MARKERS = ("待" "补充", "占" "位")
_WORD_UNFINISHED_MARKERS = ("TO" "DO", "TB" "D", "place" "holder", "x" "xx", "X" "XX")
_WORD_UNFINISHED_PATTERN = re.compile(r"\b(?:" + "|".join(_WORD_UNFINISHED_MARKERS) + r")\b")
_ARCHIVE_ORIGINAL_PATH_PATTERN = re.compile(r"`(docs/[^`]+\.md)`")
_LEGACY_GOVERNANCE_AGENT_PATTERN = re.compile(
    r"(?:attribution|proposal|execution|eval-case|regression-impact).{0,120}治理 Agent"
)


@dataclass(frozen=True)
class DocsGovernanceIssue:
    path: str
    message: str
    blocking: bool = True


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "core.quotePath=false", *args],
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="ignore",
        text=True,
    )


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


def _skill_names(root: Path, rel_root: str) -> set[str]:
    base = root / rel_root
    if not base.is_dir():
        return set()
    return {path.name for path in base.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()}


def collect_mirrored_skill_pairs(root: Path) -> tuple[tuple[str, str], ...]:
    codex_names = _skill_names(root, CODEX_SKILLS_ROOT)
    claude_names = _skill_names(root, CLAUDE_SKILLS_ROOT)
    names = sorted((codex_names | claude_names) - MIRRORED_SKILL_EXCLUSIONS)
    return tuple((f"{CODEX_SKILLS_ROOT}/{name}/SKILL.md", f"{CLAUDE_SKILLS_ROOT}/{name}/SKILL.md") for name in names)


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


def _archived_original_paths(root: Path) -> tuple[str, ...]:
    """Return archive-index original paths only when the whole cell is the path.

    Rows such as `` `docs/active.md` 旧完整稿 `` describe an archived draft of an
    active document; they must not make the active document path invalid.
    """
    index = _read_existing(root, ARCHIVE_INDEX)
    archived_paths: set[str] = set()
    for line in index.splitlines():
        if not line.startswith("|") or "docs/" not in line:
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < len(ARCHIVE_INDEX_HEADERS):
            continue
        match = _ARCHIVE_ORIGINAL_PATH_PATTERN.fullmatch(cells[0])
        if match:
            archived_paths.add(match.group(1))
    return tuple(sorted(archived_paths))


def _documentation_contract_reads_path(text: str, archived_path: str) -> bool:
    escaped = re.escape(archived_path)
    return re.search(r"_read_repo_text\(\s*['\"]" + escaped + r"['\"]\s*\)", text) is not None


def _archived_original_reference_issues(root: Path, paths: Iterable[str]) -> list[DocsGovernanceIssue]:
    archived_paths = _archived_original_paths(root)
    if not archived_paths:
        return []
    issues: list[DocsGovernanceIssue] = []
    active_doc_paths = sorted(path for path in paths if path == DOCS_INDEX or _is_active_doc(path))
    for rel_path in active_doc_paths:
        text = _read_existing(root, rel_path)
        for archived_path in archived_paths:
            if archived_path in text:
                issues.append(
                    DocsGovernanceIssue(
                        rel_path,
                        f"archived original path is still referenced from active docs: {archived_path}",
                    )
                )

    contract_path = "tests/test_documentation_contracts.py"
    if contract_path in set(paths):
        contract_text = _read_existing(root, contract_path)
        for archived_path in archived_paths:
            if _documentation_contract_reads_path(contract_text, archived_path):
                issues.append(
                    DocsGovernanceIssue(
                        contract_path,
                        f"documentation contract test still reads archived original path: {archived_path}",
                    )
                )
    return issues


def _long_term_authority_term_issues(root: Path, paths: Iterable[str]) -> list[DocsGovernanceIssue]:
    issues: list[DocsGovernanceIssue] = []
    for rel_path in sorted(set(paths) & LONG_TERM_AUTHORITY_DOCS):
        text = _read_existing(root, rel_path)
        for line_number, line in enumerate(text.splitlines(), start=1):
            legacy_term = next((term for term in LEGACY_GOVERNANCE_AGENT_TERMS if term in line), None)
            if legacy_term or _LEGACY_GOVERNANCE_AGENT_PATTERN.search(line):
                issues.append(
                    DocsGovernanceIssue(
                        rel_path,
                        "long-term authority doc uses legacy governance-agent terminology "
                        f"at line {line_number}; use `governor` plus job type",
                    )
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
    for codex_path, claude_path in collect_mirrored_skill_pairs(root):
        codex_text = _read_existing(root, codex_path)
        claude_text = _read_existing(root, claude_path)
        if bool(codex_text) != bool(claude_text):
            missing_path = codex_path if not codex_text else claude_path
            anchor_path = claude_path if not codex_text else codex_path
            issues.append(DocsGovernanceIssue(anchor_path, f"mirrored skill pair is incomplete: missing {missing_path}"))
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


def _new_or_changed_lines(root: Path, base_ref: str | None, rel_path: str) -> list[tuple[int, str]]:
    current_lines = _read_existing(root, rel_path).splitlines()
    if base_ref is None:
        return list(enumerate(current_lines, start=1))
    base_text = _git_show(root, base_ref, rel_path)
    if base_text is None:
        return list(enumerate(current_lines, start=1))
    unchanged_lines = set(base_text.splitlines())
    return [(line_number, line) for line_number, line in enumerate(current_lines, start=1) if line not in unchanged_lines]


def _unfinished_marker_issues(root: Path, base_ref: str | None, paths: Iterable[str]) -> list[DocsGovernanceIssue]:
    issues: list[DocsGovernanceIssue] = []
    changed_paths = sorted(path for path in paths if _text_governed_path(path) and _is_changed_path(root, base_ref, path))
    for rel_path in changed_paths:
        for line_number, line in _new_or_changed_lines(root, base_ref, rel_path):
            marker = _find_unfinished_marker(line)
            if marker is not None:
                issues.append(DocsGovernanceIssue(rel_path, f"unfinished marker `{marker}` at line {line_number}"))
    return issues


def _local_artifact_path_issues(root: Path, base_ref: str | None, paths: Iterable[str]) -> list[DocsGovernanceIssue]:
    issues: list[DocsGovernanceIssue] = []
    changed_paths = sorted(path for path in paths if _text_governed_path(path) and _is_changed_path(root, base_ref, path))
    for rel_path in changed_paths:
        for line_number, line in _new_or_changed_lines(root, base_ref, rel_path):
            marker = next((item for item in LOCAL_ARTIFACT_PATH_MARKERS if item in line), None)
            if marker is not None:
                issues.append(DocsGovernanceIssue(rel_path, f"local artifact path `{marker}` at line {line_number}"))
    return issues


def collect_docs_governance_issues(root: Path, base_ref: str | None) -> list[DocsGovernanceIssue]:
    paths = _repo_paths(root)
    new_paths = _new_paths(root, base_ref, paths)
    new_active_docs = [path for path in new_paths if _is_active_doc(path)]
    new_archive_docs = [path for path in new_paths if _is_archive_doc(path)]
    issues: list[DocsGovernanceIssue] = []
    issues.extend(_active_doc_index_issues(root, new_active_docs))
    issues.extend(_archive_index_issues(root, new_archive_docs))
    issues.extend(_archived_original_reference_issues(root, paths))
    issues.extend(_long_term_authority_term_issues(root, paths))
    issues.extend(_skill_mirror_issues(root))
    issues.extend(_unfinished_marker_issues(root, base_ref, paths))
    issues.extend(_local_artifact_path_issues(root, base_ref, paths))
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
