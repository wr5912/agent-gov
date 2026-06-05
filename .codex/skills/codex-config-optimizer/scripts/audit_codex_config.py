#!/usr/bin/env python3
"""只读审计 Codex 配置面，输出可迁移、删重或脚本化建议。"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SURFACE_PATTERNS = (
    "AGENTS.md",
    "AGENTS.override.md",
    ".codex/README.md",
    ".codex/config.toml",
    ".codex/hooks.json",
    ".codex/rules/*.rules",
    ".codex/skills/*/SKILL.md",
)

HOT_TERMS = (
    "字段所有权矩阵",
    "执行动作矩阵",
    "治理硬门",
    "JsonObject",
    "schema_version",
    "coverage policy",
    "main-flow",
    "make main-flow-test",
    "check_codex_governance.py",
)

TRIGGER_WORDS = (
    "当",
    "提到",
    "涉及",
    "用户",
    "使用",
    "编写",
    "评审",
    "调试",
    "重构",
    "优化",
    "审计",
    "配置",
    "skill",
    "Codex",
)


@dataclass(frozen=True)
class Issue:
    severity: str
    path: str
    line: int | None
    message: str
    action: str


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in SURFACE_PATTERNS:
        files.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(set(files))


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _line_count(text: str) -> int:
    return len(text.splitlines())


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    data: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def _find_line(text: str, needle: str) -> int | None:
    for index, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return index
    return None


def _audit_size(root: Path, path: Path, text: str) -> Iterable[Issue]:
    rel = _relative(root, path)
    lines = _line_count(text)
    if path.name == "SKILL.md" and lines > 500:
        yield Issue("P1", rel, None, f"SKILL.md 有 {lines} 行，触发渐进披露风险。", "move-to-skill")
    if path.suffix == ".rules" and lines > 220:
        yield Issue("P1", rel, None, f"rules 文件有 {lines} 行，常驻治理说明可能过重。", "merge")
    if rel in {"AGENTS.md", "AGENTS.override.md"} and lines > 260:
        yield Issue("P2", rel, None, f"常驻说明有 {lines} 行，建议审计是否能迁入 skill。", "move-to-skill")


def _audit_skill(root: Path, path: Path, text: str) -> Iterable[Issue]:
    if path.name != "SKILL.md":
        return
    rel = _relative(root, path)
    frontmatter = _frontmatter(text)
    name = frontmatter.get("name", "")
    description = frontmatter.get("description", "")
    if not name:
        yield Issue("P0", rel, 1, "缺少 frontmatter name。", "keep")
    elif not re.fullmatch(r"[a-z0-9-]{1,64}", name):
        yield Issue("P0", rel, 1, f"name `{name}` 不符合 kebab-case 或长度约束。", "keep")
    if not description:
        yield Issue("P0", rel, 1, "缺少 frontmatter description。", "keep")
    elif len(description) > 1024:
        yield Issue("P1", rel, 1, "description 超过 1024 字符，触发信息可能被截断。", "merge")
    elif not any(word in description for word in TRIGGER_WORDS):
        yield Issue("P1", rel, 1, "description 缺少明显触发词，可能不易被隐式调用。", "merge")

    for match in re.finditer(r"\[[^\]]+\]\(([^)]+\.md)\)", text):
        target = match.group(1)
        if target.count("/") > 1:
            line = _find_line(text, target)
            yield Issue("P2", rel, line, f"引用 `{target}` 层级较深，建议从 SKILL.md 直接引用一级文件。", "move-to-skill")


def _audit_nested_references(root: Path, path: Path, text: str) -> Iterable[Issue]:
    rel = _relative(root, path)
    if "/references/" not in rel:
        return
    for match in re.finditer(r"\[[^\]]+\]\(([^)]+\.md)\)", text):
        target = match.group(1)
        line = _find_line(text, target)
        yield Issue("P2", rel, line, f"reference 内继续引用 `{target}`，可能形成深层披露路径。", "merge")


def _audit_config(root: Path, path: Path, text: str) -> Iterable[Issue]:
    rel = _relative(root, path)
    if rel != ".codex/config.toml":
        return
    if re.search(r"(?m)^\s*model\s*=", text):
        yield Issue("P1", rel, _find_line(text, "model"), "项目配置固定了 model，可能混入个人偏好。", "delete")
    if re.search(r"API_KEY|TOKEN|SECRET|PASSWORD", text, re.IGNORECASE):
        yield Issue("P0", rel, None, "项目配置疑似包含敏感变量名。", "delete")


def _audit_rules(root: Path, path: Path, text: str) -> Iterable[Issue]:
    if path.suffix != ".rules":
        return
    rel = _relative(root, path)
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            yield Issue("P0", rel, index, ".rules 文件包含非注释正文，可能被 Starlark 解析失败。", "keep")
            break


def _term_report(root: Path, files: list[Path]) -> list[tuple[str, list[str], int]]:
    report: list[tuple[str, list[str], int]] = []
    for term in HOT_TERMS:
        paths: list[str] = []
        total = 0
        for path in files:
            text = _read(path)
            count = text.count(term)
            if count:
                paths.append(_relative(root, path))
                total += count
        if len(paths) >= 3 or total >= 5:
            report.append((term, paths, total))
    return report


def _collect_issues(root: Path, files: list[Path]) -> list[Issue]:
    issues: list[Issue] = []
    for path in files:
        text = _read(path)
        issues.extend(_audit_size(root, path, text))
        issues.extend(_audit_skill(root, path, text))
        issues.extend(_audit_nested_references(root, path, text))
        issues.extend(_audit_config(root, path, text))
        issues.extend(_audit_rules(root, path, text))
    return sorted(issues, key=lambda issue: (issue.severity, issue.path, issue.line or 0))


def _print_report(root: Path) -> None:
    files = _iter_files(root)
    issues = _collect_issues(root, files)

    print("# Codex 配置审计报告")
    print()
    print(f"- root: `{root}`")
    print(f"- surfaces: {len(files)}")
    print()
    print("## 配置面")
    print()
    for path in files:
        text = _read(path)
        print(f"- `{_relative(root, path)}`: {_line_count(text)} 行")

    print()
    print("## 问题")
    print()
    if not issues:
        print("- 未发现 P0/P1/P2 静态问题。")
    for issue in issues:
        line = f":{issue.line}" if issue.line else ""
        print(f"- `{issue.severity}` `{issue.path}{line}` {issue.message} 建议动作：`{issue.action}`。")

    term_report = _term_report(root, files)
    print()
    print("## 高频治理词")
    print()
    if not term_report:
        print("- 未发现跨多配置面的高频治理词。")
    for term, paths, total in term_report:
        path_list = ", ".join(f"`{path}`" for path in paths)
        print(f"- `{term}` 出现 {total} 次，涉及 {path_list}。建议检查是否可 `merge` 或 `move-to-skill`。")


def main() -> int:
    parser = argparse.ArgumentParser(description="只读审计 Codex 配置面。")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="仓库根目录，默认当前目录。")
    args = parser.parse_args()
    _print_report(args.root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
