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
    ".claude/README.md",
    ".claude/settings.json",
    ".claude/rules/*.md",
    ".claude/skills/*/SKILL.md",
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

TERM_GUIDANCE = {
    "字段所有权矩阵": ("merge", "按需 skill/reference 保留模板；常驻 rules 只保留触发入口", "专项任务开始前能产出字段所有权矩阵，审计重复词下降"),
    "执行动作矩阵": ("move-to-skill", "按需预检模板", "用户可见工作流改动前能列出动作与用户任务映射"),
    "治理硬门": ("merge", "项目覆盖层保留命令；skill/reference 保留解释", "审计报告仍能定位硬门命令，但常驻说明不重复展开"),
    "JsonObject": ("move-to-skill", "typed-output / Agent 契约预检", "新增主流程不增加弱 JsonObject 边界"),
    "schema_version": ("move-to-skill", "typed-output / Agent 契约预检", "无外部协议或迁移需求时不新增输出 schema version"),
    "coverage policy": ("keep", "测试覆盖清单", "主流程场景绑定到 pytest nodeid 或 UI verification script"),
    "main-flow": ("keep", "测试覆盖清单与验证规则", "主流程改动运行 make main-flow-test"),
    "make main-flow-test": ("keep", "项目覆盖层验证入口", "主流程验证命令可执行"),
    "check_codex_governance.py": ("keep", "项目覆盖层治理硬门", "fail 模式治理检查可执行"),
}

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

ENV_CONTEXT_TERMS = (
    ".env",
    "env",
    "环境变量",
    "RUNTIME",
    "local-debug",
    "PyCharm",
    "Compose",
    "Docker",
    "settings_env_file",
    "MODEL_PROVIDER_API_KEY",
    "Langfuse",
)

ENV_OVERRIDE_TERMS = (
    "本地私有覆盖",
    "私有覆盖",
    "覆盖文件",
    "覆盖配置",
    "覆盖 env",
    "覆盖环境",
    "覆盖关系",
    "local override",
    "local overrides",
    "override file",
)

ENV_TERMINOLOGY_NEGATIONS = ("不要", "不得", "不是", "不应", "不能", "除非", "禁止")
ENV_TERMINOLOGY_COVERAGE_CONTEXTS = ("测试覆盖", "覆盖 env 文件选择", "覆盖清单", "覆盖率", "覆盖策略", "coverage policy")

MATRIX_EXPECTATIONS = (
    (
        ".codex/skills/codex-config-optimizer/SKILL.md",
        "配置治理三矩阵",
        ("治理对象矩阵", "配置面矩阵", "验收矩阵"),
        "move-to-skill",
        "配置优化请求先分类对象、配置面和验收方式，再决定是否改常驻规则。",
    ),
    (
        ".codex/skills/business-agent-workspace-optimizer/SKILL.md",
        "业务 Agent 目标解析矩阵",
        ("目标解析矩阵", "不得把 `${RUNTIME_ROOT}/data`", "单个业务 Agent"),
        "move-to-skill",
        "业务 Agent workspace 修改前能证明目标精确到单个 workspace，父目录不是目标。",
    ),
    (
        ".codex/skills/runtime-env-governance/SKILL.md",
        "runtime/env 测试模式选择矩阵",
        ("测试模式选择矩阵", "make container-live-test", "docker/.env.local-debug"),
        "move-to-skill",
        "live 验收必须走 Docker Compose 容器和 docker/.env；local-debug 仅服务专项调试测试。",
    ),
    (
        ".codex/skills/test-sync-governance/SKILL.md",
        "改动类型到验证命令矩阵",
        ("改动类型", "推荐验证命令", "不默认跑全量"),
        "move-to-skill",
        "配置、文档、skill、主流程和发版场景能映射到不同测试深度。",
    ),
    (
        ".codex/skills/agentgov-closeout-sync/SKILL.md",
        "发版收尾检查矩阵",
        ("发版收尾检查矩阵", "版本面", "远端校验"),
        "move-to-skill",
        "发版前后同步 README、docs、skill 镜像、版本面和远端状态。",
    ),
)


@dataclass(frozen=True)
class Issue:
    severity: str
    path: str
    line: int | None
    message: str
    action: str


@dataclass(frozen=True)
class TerminologyRisk:
    path: str
    line: int
    term: str
    excerpt: str
    suggestion: str


@dataclass(frozen=True)
class MatrixCoverage:
    path: str
    label: str
    missing_markers: tuple[str, ...]
    action: str
    verification: str


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


def _term_guidance(term: str) -> tuple[str, str, str]:
    return TERM_GUIDANCE.get(term, ("merge", "按需 skill 或 reference", "审计后确认重复配置已减少"))


def _terminology_risks(root: Path, files: list[Path]) -> list[TerminologyRisk]:
    risks: list[TerminologyRisk] = []
    for path in files:
        rel = _relative(root, path)
        text = _read(path)
        for line_number, line in enumerate(text.splitlines(), start=1):
            if any(coverage_context in line for coverage_context in ENV_TERMINOLOGY_COVERAGE_CONTEXTS):
                continue
            if any(negation in line for negation in ENV_TERMINOLOGY_NEGATIONS):
                continue
            if not any(context in line for context in ENV_CONTEXT_TERMS):
                continue
            matched_term = next((term for term in ENV_OVERRIDE_TERMS if term in line), None)
            if not matched_term:
                continue
            excerpt = line.strip()
            if len(excerpt) > 140:
                excerpt = f"{excerpt[:137]}..."
            risks.append(
                TerminologyRisk(
                    path=rel,
                    line=line_number,
                    term=matched_term,
                    excerpt=excerpt,
                    suggestion="如果代码没有 layered override，请改成“选择 env 文件”“私有 env 文件”或“本机调试 env 文件”。",
                )
            )
    return risks


def _matrix_coverage(root: Path) -> list[MatrixCoverage]:
    coverage: list[MatrixCoverage] = []
    for rel_path, label, markers, action, verification in MATRIX_EXPECTATIONS:
        path = root / rel_path
        text = _read(path) if path.is_file() else ""
        missing = tuple(marker for marker in markers if marker not in text)
        coverage.append(MatrixCoverage(rel_path, label, missing, action, verification))
    return coverage


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
        action, target_surface, verification = _term_guidance(term)
        print(
            f"- `{term}` 出现 {total} 次，涉及 {path_list}。"
            f"建议动作：`{action}`；目标配置面：{target_surface}；验证：{verification}。"
        )

    terminology_risks = _terminology_risks(root, files)
    print()
    print("## 术语风险")
    print()
    if not terminology_risks:
        print("- 未发现 env/runtime 语境下的“覆盖”术语风险。")
    for risk in terminology_risks:
        print(
            f"- `{risk.path}:{risk.line}` 命中 `{risk.term}`：{risk.excerpt} "
            f"建议：{risk.suggestion}"
        )

    matrix_coverage = _matrix_coverage(root)
    print()
    print("## 三矩阵覆盖")
    print()
    for coverage in matrix_coverage:
        if not coverage.missing_markers:
            print(f"- OK `{coverage.path}` 覆盖 {coverage.label}。验证：{coverage.verification}")
            continue
        missing = "、".join(f"`{marker}`" for marker in coverage.missing_markers)
        print(
            f"- MISSING `{coverage.path}` 缺少 {coverage.label} 标记：{missing}。"
            f"建议动作：`{coverage.action}`；验证：{coverage.verification}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="只读审计 Codex 配置面。")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="仓库根目录，默认当前目录。")
    args = parser.parse_args()
    _print_report(args.root.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
