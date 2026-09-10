#!/usr/bin/env python3
"""校验 AgentScope-only Harness 与离线转换证据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentgov_harness_digest import harness_content_digest  # noqa: E402

DEFAULT_BOOTSTRAP_ROOT = REPO_ROOT / "docker" / "runtime-bootstrap"
RUNTIME_CONTRACT = "agentscope-app/2.0.8"
CONVERTER_ID = "agentgov-claude-to-agentscope/v1"
ENV_REFERENCE_RE = re.compile(r"\$\{(?P<name>[A-Z][A-Z0-9_]*)\}")
LEGACY_ENTRY_NAMES = {".claude", ".mcp.json", "CLAUDE.md", "hooks"}
LEGACY_ACTIVE_PATTERNS = {
    "claude-agent-sdk": re.compile(r"claude-agent-sdk", re.IGNORECASE),
    "ClaudeRuntime": re.compile(r"\bClaudeRuntime\b"),
    "sdk_session_id": re.compile(r"\bsdk_session_id\b"),
    "claude.sdk": re.compile(r"\bclaude\.sdk\."),
    "CLAUDE_": re.compile(r"\bCLAUDE_[A-Z0-9_]*"),
    "AGENT_RUNTIME_KIND": re.compile(r"\bAGENT_RUNTIME_KIND\b"),
    ".claude/": re.compile(r"\.claude/"),
    "hooks": re.compile(r"\bhooks\b"),
    "mcp_servers/": re.compile(r"\bmcp_servers/"),
}
STATIC_CUTOVER_ROOTS = (
    Path("agentscope_runtime"),
    Path("app"),
    Path("frontend/src"),
    Path("integrations"),
    Path("docker"),
    Path("agentgov_harness_digest.py"),
    Path("Makefile"),
    Path("README.md"),
    Path("requirements.txt"),
    Path("requirements-api.txt"),
)
_PRIVATE_AGENTSCOPE_IMPORT = re.compile(
    r"(?:from|import)\s+agentscope(?:\.[A-Za-z0-9_]+)*\._[A-Za-z0-9_.]*",
)
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache"}
HarnessObject = dict[str, object]


@dataclass(frozen=True)
class Finding:
    path: str
    code: str
    message: str


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_digest(root: Path, *, excluded: tuple[str, ...] = ()) -> str:
    excluded_paths = set(excluded)
    digest = hashlib.sha256()
    for path in _files(root):
        relative = path.relative_to(root).as_posix()
        if relative in excluded_paths:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def check_bootstrap(root: Path = DEFAULT_BOOTSTRAP_ROOT) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []
    if root.is_symlink() or not root.is_dir():
        return [Finding(str(root), "bootstrap_missing", "bootstrap root 必须是普通目录")]
    findings.extend(_legacy_entry_findings(root))
    findings.extend(_legacy_text_findings(root))
    workspaces = _workspace_roots(root)
    if not workspaces:
        findings.append(Finding(str(root), "workspace_missing", "未发现 AgentScope Harness"))
        return findings
    for workspace in workspaces:
        findings.extend(check_workspace(workspace))
    return sorted(findings, key=lambda item: (item.path, item.code, item.message))


def check_workspace(workspace: Path) -> list[Finding]:
    findings: list[Finding] = []
    manifest_path = workspace / "agent.yaml"
    prompt_path = workspace / "AGENT.md"
    report_path = workspace / "conversion-report.json"
    for required in (manifest_path, prompt_path, report_path, workspace / "tests"):
        if not required.exists():
            findings.append(_finding(workspace, required, "required_asset_missing", "缺少必需 Harness 资产"))
    if findings:
        return findings
    manifest = _load_object(manifest_path, findings, workspace)
    if manifest is not None:
        findings.extend(_check_manifest(workspace, manifest))
    report = _load_object(report_path, findings, workspace)
    if report is not None:
        findings.extend(_check_report(workspace, report))
    return findings


def check_production_does_not_call_converter(repo_root: Path = REPO_ROOT) -> list[Finding]:
    findings: list[Finding] = []
    candidates = [repo_root / "app", repo_root / "docker", repo_root / "Makefile"]
    for candidate in candidates:
        paths = _files(candidate) if candidate.is_dir() else ((candidate,) if candidate.is_file() else ())
        for path in paths:
            if path.name == "conversion-report.json":
                continue
            text = _read_text(path)
            if text is not None and "convert_claude_harness" in text:
                findings.append(Finding(path.relative_to(repo_root).as_posix(), "production_converter_call", "生产路径不得调用离线转换器"))
    return findings


def check_static_cutover(repo_root: Path = REPO_ROOT) -> list[Finding]:
    """Enforce the AgentScope-only production surface, not only bootstrap assets."""

    findings: list[Finding] = []
    runtime_clients: list[str] = []
    for relative_root in STATIC_CUTOVER_ROOTS:
        candidate = repo_root / relative_root
        paths = _files(candidate) if candidate.is_dir() else ((candidate,) if candidate.is_file() else ())
        for path in paths:
            relative = path.relative_to(repo_root)
            if path.name == "conversion-report.json" or path.name.startswith(".env.bak-"):
                continue
            text = _read_text(path)
            if text is None:
                continue
            for name, pattern in LEGACY_ACTIVE_PATTERNS.items():
                # Broader bootstrap-only terms are migration evidence, not the
                # six production runtime hard gates from the cutover plan.
                if name in {".claude/", "hooks", "mcp_servers/"}:
                    continue
                if pattern.search(text):
                    findings.append(
                        Finding(
                            relative.as_posix(),
                            "legacy_runtime_reference",
                            f"AgentScope-only 生产面包含旧 Runtime 引用: {name}",
                        ),
                    )
            if relative.parts and relative.parts[0] in {"app", "agentscope_runtime"}:
                if _PRIVATE_AGENTSCOPE_IMPORT.search(text):
                    findings.append(
                        Finding(
                            relative.as_posix(),
                            "private_agentscope_import",
                            "生产代码不得导入 AgentScope 私有模块",
                        ),
                    )
                runtime_clients.extend(
                    f"{relative.as_posix()}:{match.group(1)}" for match in re.finditer(r"^class\s+([A-Za-z0-9_]*RuntimeClient)\b", text, re.MULTILINE)
                )
    if runtime_clients != ["app/runtime_gateway/client.py:AgentScopeRuntimeClient"]:
        findings.append(
            Finding(
                "app/runtime_gateway/client.py",
                "runtime_implementation_count",
                f"生产 Runtime client 必须且只能有 AgentScopeRuntimeClient，实际为 {runtime_clients}",
            ),
        )
    findings.extend(_fresh_schema_name_findings())
    return findings


def _fresh_schema_name_findings() -> list[Finding]:
    repo_path = str(REPO_ROOT)
    if repo_path not in sys.path:
        sys.path.insert(0, repo_path)
    try:
        from app.runtime.runtime_db import Base
    except Exception as exc:  # noqa: BLE001 - a broken schema import is itself a cutover failure
        return [Finding("app/runtime/runtime_db.py", "schema_import", f"无法读取 fresh schema: {exc.__class__.__name__}")]
    findings: list[Finding] = []
    for table_name, table in Base.metadata.tables.items():
        names = [table_name, *(column.name for column in table.columns)]
        banned = [name for name in names if "claude" in name.casefold() or re.search(r"(^|_)sdk(_|$)", name.casefold())]
        if banned:
            findings.append(
                Finding(
                    "app/runtime/runtime_db.py",
                    "legacy_schema_name",
                    f"fresh schema {table_name} 含旧 Runtime 表/列: {sorted(banned)}",
                ),
            )
    return findings


def _check_manifest(workspace: Path, manifest: HarnessObject) -> list[Finding]:
    findings: list[Finding] = []
    agent = manifest.get("agent")
    session = manifest.get("session")
    policy = manifest.get("workspace_policy")
    harness = manifest.get("harness")
    if manifest.get("schema_version") != 1:
        findings.append(_manifest_finding(workspace, "manifest_schema", "schema_version 必须为 1"))
    if not isinstance(agent, dict):
        return findings + [_manifest_finding(workspace, "agent_contract", "agent 必须是 object")]
    if not isinstance(agent.get("id"), str) or not agent["id"]:
        findings.append(_manifest_finding(workspace, "agent_id", "agent.id 必须是非空字符串"))
    if agent.get("runtime") != "agentscope" or agent.get("runtime_contract") != RUNTIME_CONTRACT:
        findings.append(_manifest_finding(workspace, "runtime_contract", "Runtime 必须固定为 AgentScope 2.0.8 公共契约"))
    if agent.get("system_prompt") != "AGENT.md":
        findings.append(_manifest_finding(workspace, "system_prompt", "system_prompt 必须指向 AGENT.md"))
    if not isinstance(session, dict) or session.get("permission_mode") not in {"default", "explore", "accept_edits", "dont_ask"}:
        findings.append(_manifest_finding(workspace, "permission_mode", "Session permission_mode 非法或缺失"))
    if isinstance(session, dict) and "workspace_id" in session:
        findings.append(_manifest_finding(workspace, "workspace_id", "workspace_id 属于 Session binding，不得写入 Harness"))
    if not isinstance(policy, dict) or not policy.get("fail_closed") or not policy.get("immutable_harness"):
        findings.append(_manifest_finding(workspace, "workspace_policy", "Workspace policy 必须 fail-closed 且 Harness 不可变"))
    if isinstance(policy, dict) and policy.get("allow_for_run") is not False:
        findings.append(_manifest_finding(workspace, "run_permission", "bootstrap Harness 不得默认整轮放权"))
    if isinstance(policy, dict):
        allowed_tools = policy.get("allowed_tools")
        if not isinstance(allowed_tools, list) or any(
            isinstance(item, str) and item.partition("(")[0].startswith("mcp__") and any(character in item.partition("(")[0] for character in "*?[")
            for item in allowed_tools
        ):
            findings.append(_manifest_finding(workspace, "mcp_permission", "MCP allow 规则必须使用精确工具名"))
    if not isinstance(harness, dict):
        return findings + [_manifest_finding(workspace, "harness_contract", "harness 必须是 object")]
    if harness.get("content_digest") != _harness_content_digest(workspace):
        findings.append(_manifest_finding(workspace, "harness_digest", "Harness digest 与实际资产不一致"))
    findings.extend(_check_asset_record(workspace, harness.get("system_prompt"), "system_prompt"))
    findings.extend(_check_asset_record(workspace, harness.get("tests"), "tests"))
    for group in ("skills", "mcps", "subagents"):
        records = harness.get(group)
        if not isinstance(records, list):
            findings.append(_manifest_finding(workspace, "asset_group", f"harness.{group} 必须是 list"))
            continue
        for record in records:
            findings.extend(_check_asset_record(workspace, record, group))
    findings.extend(_check_skills(workspace))
    findings.extend(_check_mcps(workspace))
    findings.extend(_check_subagents(workspace))
    return findings


def _check_asset_record(workspace: Path, record: Any, group: str) -> list[Finding]:
    if not isinstance(record, dict):
        return [_manifest_finding(workspace, "asset_record", f"{group} 资产记录必须是 object")]
    relative = record.get("path")
    expected = record.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        return [_manifest_finding(workspace, "asset_record", f"{group} 缺少 path/sha256")]
    target = _safe_target(workspace, relative)
    if target is None or not target.exists() or target.is_symlink():
        return [_manifest_finding(workspace, "asset_path", f"{group} 资产路径非法或不存在: {relative}")]
    actual = sha256_file(target) if target.is_file() else tree_digest(target)
    if actual != expected:
        return [_manifest_finding(workspace, "asset_digest", f"{group} 资产 digest 不匹配: {relative}")]
    return []


def _check_skills(workspace: Path) -> list[Finding]:
    findings: list[Finding] = []
    skills = workspace / "skills"
    if not skills.exists():
        return findings
    for skill_md in sorted(skills.glob("*/SKILL.md")):
        text = skill_md.read_text(encoding="utf-8")
        metadata = _frontmatter(text)
        if not metadata.get("name") or not metadata.get("description"):
            findings.append(_finding(workspace, skill_md, "skill_frontmatter", "Skill 缺少 name/description"))
    return findings


def _check_mcps(workspace: Path) -> list[Finding]:
    findings: list[Finding] = []
    mcp_root = workspace / "mcp"
    if not mcp_root.exists():
        return findings
    for mcp_path in sorted(mcp_root.glob("*.json")):
        payload = _load_object(mcp_path, findings, workspace)
        if payload is None:
            continue
        config = payload.get("mcp_config")
        references = payload.get("credential_refs")
        if not isinstance(config, dict) or config.get("type") != "http_mcp":
            findings.append(_finding(workspace, mcp_path, "mcp_contract", "MCP config 必须使用 http_mcp transport"))
            continue
        if not isinstance(references, list):
            findings.append(_finding(workspace, mcp_path, "credential_refs", "MCP 必须声明 credential_refs"))
            continue
        declared = {(item.get("path"), item.get("env")) for item in references if isinstance(item, dict)}
        actual = set(_credential_references(config))
        if declared != actual:
            findings.append(_finding(workspace, mcp_path, "credential_refs", "credential_refs 与占位引用不一致"))
        headers = config.get("headers", {})
        if isinstance(headers, dict):
            for key, value in headers.items():
                if re.search(r"authorization|token|secret|key", str(key), re.IGNORECASE) and not ENV_REFERENCE_RE.search(str(value)):
                    findings.append(_finding(workspace, mcp_path, "inline_secret", f"敏感 Header 必须只保存 env 引用: {key}"))
        capabilities = {field: payload.get(field) for field in ("enable_tools", "enable_resources", "enable_resource_templates")}
        if any(
            not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value) or len(value) != len(set(value))
            for value in capabilities.values()
        ):
            findings.append(_finding(workspace, mcp_path, "mcp_allowlist", "MCP 能力必须使用显式唯一字符串清单"))
            continue
        tools = capabilities["enable_tools"]
        assert isinstance(tools, list)
        if any(any(character in tool for character in "*?[") for tool in tools):
            findings.append(_finding(workspace, mcp_path, "mcp_allowlist", "enable_tools 不得包含通配符"))
        if payload.get("disable_tools") not in (None, []):
            findings.append(_finding(workspace, mcp_path, "mcp_allowlist", "精确 enable_tools 下禁止 disable_tools"))
    return findings


def _check_subagents(workspace: Path) -> list[Finding]:
    findings: list[Finding] = []
    root = workspace / "subagents"
    if not root.exists():
        return findings
    for subagent in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest_path = subagent / "agent.yaml"
        prompt_path = subagent / "AGENT.md"
        if not manifest_path.is_file() or not prompt_path.is_file():
            findings.append(_finding(workspace, subagent, "subagent_contract", "subagent 缺少 agent.yaml/AGENT.md"))
            continue
        manifest = _load_object(manifest_path, findings, workspace)
        agent = manifest.get("agent") if manifest else None
        if not isinstance(agent, dict) or agent.get("runtime") != "agentscope" or agent.get("runtime_contract") != RUNTIME_CONTRACT:
            findings.append(_finding(workspace, manifest_path, "subagent_runtime", "subagent Runtime 契约非法"))
        policy = manifest.get("workspace_policy") if manifest else None
        allowed_tools = policy.get("allowed_tools") if isinstance(policy, dict) else None
        if not isinstance(allowed_tools, list) or any(
            isinstance(item, str) and item.partition("(")[0].startswith("mcp__") and any(character in item.partition("(")[0] for character in "*?[")
            for item in allowed_tools
        ):
            findings.append(_finding(workspace, manifest_path, "mcp_permission", "Subagent MCP allow 规则必须使用精确工具名"))
    return findings


def _check_report(workspace: Path, report: HarnessObject) -> list[Finding]:
    findings: list[Finding] = []
    if report.get("converter") != CONVERTER_ID:
        findings.append(_report_finding(workspace, "converter", "转换器版本不匹配"))
    if report.get("source_coverage_percent") != 100.0 or report.get("rejected_count") != 0:
        findings.append(_report_finding(workspace, "coverage", "source coverage 必须 100% 且 rejected=0"))
    manifest = _load_object(workspace / "agent.yaml", findings, workspace)
    harness = manifest.get("harness") if manifest else None
    if not isinstance(harness, dict) or report.get("harness_digest") != harness.get("content_digest"):
        findings.append(_report_finding(workspace, "harness_digest", "conversion report 必须绑定 manifest Harness digest"))
    entries = report.get("entries")
    if not isinstance(entries, list) or len(entries) != report.get("source_file_count"):
        findings.append(_report_finding(workspace, "entry_count", "映射明细数量与源文件数不一致"))
        return findings
    mapped_count = sum(isinstance(entry, dict) and entry.get("status") == "mapped" for entry in entries)
    retired_count = sum(isinstance(entry, dict) and entry.get("status") == "retired" for entry in entries)
    if report.get("mapped_count") != mapped_count or report.get("retired_count") != retired_count:
        findings.append(_report_finding(workspace, "classified_count", "mapped/retired 计数必须覆盖全部源文件"))
    source_paths = [entry.get("source_path") for entry in entries if isinstance(entry, dict)]
    if len(source_paths) != len(set(source_paths)):
        findings.append(_report_finding(workspace, "duplicate_source", "source_path 必须唯一"))
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("status") not in {"mapped", "retired"}:
            findings.append(_report_finding(workspace, "mapping_status", "映射状态只能是 mapped/retired"))
            continue
        if entry["status"] == "mapped" and not entry.get("targets"):
            findings.append(_report_finding(workspace, "mapping_target", "mapped 源文件必须关联转换目标"))
        for target_record in entry.get("targets", []):
            findings.extend(_check_report_target(workspace, target_record))
    expected_tree = report.get("output_tree_sha256")
    actual_tree = tree_digest(workspace, excluded=("conversion-report.json",))
    if expected_tree != actual_tree:
        findings.append(_report_finding(workspace, "output_digest", "输出树 digest 与 conversion report 不一致"))
    return findings


def _check_report_target(workspace: Path, record: Any) -> list[Finding]:
    if not isinstance(record, dict):
        return [_report_finding(workspace, "target_record", "target 必须是 object")]
    relative = record.get("path")
    expected = record.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        return [_report_finding(workspace, "target_record", "target 缺少 path/sha256")]
    target = _safe_target(workspace, relative)
    if target is None or not target.is_file() or sha256_file(target) != expected:
        return [_report_finding(workspace, "target_digest", f"转换目标不存在或 digest 不匹配: {relative}")]
    return []


def _workspace_roots(root: Path) -> tuple[Path, ...]:
    workspaces: list[Path] = []
    governor = root / "governor-workspace"
    if governor.is_dir():
        workspaces.append(governor)
    agents = root / "business-agents"
    if agents.is_dir():
        workspaces.extend(sorted(path / "workspace" for path in agents.iterdir() if (path / "workspace").is_dir()))
    return tuple(workspaces)


def _legacy_entry_findings(root: Path) -> list[Finding]:
    return [
        Finding(path.relative_to(root).as_posix(), "legacy_entry", "活动 bootstrap 中仍存在 Claude 专属入口")
        for path in root.rglob("*")
        if path.name in LEGACY_ENTRY_NAMES
    ]


def _legacy_text_findings(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in _files(root):
        if path.name == "conversion-report.json":
            continue
        text = _read_text(path)
        if text is None:
            continue
        for name, pattern in LEGACY_ACTIVE_PATTERNS.items():
            if pattern.search(text):
                findings.append(Finding(path.relative_to(root).as_posix(), "legacy_text", f"活动资产包含旧引用: {name}"))
    return findings


def _credential_references(value: Any, path: str = "mcp_config") -> list[tuple[str, str]]:
    references: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in sorted(value.items()):
            references.extend(_credential_references(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            references.extend(_credential_references(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        references.extend((path, match.group("name")) for match in ENV_REFERENCE_RE.finditer(value))
    return references


def _harness_content_digest(workspace: Path) -> str:
    return harness_content_digest(workspace)


def _load_object(path: Path, findings: list[Finding], workspace: Path) -> HarnessObject | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.suffix == ".json" else yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        findings.append(_finding(workspace, path, "parse_error", f"无法解析 object: {exc}"))
        return None
    if not isinstance(value, dict):
        findings.append(_finding(workspace, path, "parse_error", "根节点必须是 object"))
        return None
    return value


def _frontmatter(text: str) -> HarnessObject:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}
    value = yaml.safe_load(text[4:end]) or {}
    return value if isinstance(value, dict) else {}


def _safe_target(workspace: Path, relative: str) -> Path | None:
    target = (workspace / relative).resolve()
    return target if target == workspace or workspace in target.parents else None


def _files(root: Path) -> tuple[Path, ...]:
    if root.is_file():
        return (root,)
    return tuple(
        sorted(
            path
            for path in root.rglob("*")
            if path.is_file() and not any(part in IGNORED_PARTS for part in path.relative_to(root).parts) and path.suffix != ".pyc"
        ),
    )


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _finding(workspace: Path, path: Path, code: str, message: str) -> Finding:
    return Finding(path.relative_to(workspace).as_posix(), code, message)


def _manifest_finding(workspace: Path, code: str, message: str) -> Finding:
    return Finding((workspace / "agent.yaml").relative_to(workspace).as_posix(), code, message)


def _report_finding(workspace: Path, code: str, message: str) -> Finding:
    return Finding((workspace / "conversion-report.json").relative_to(workspace).as_posix(), code, message)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 AgentScope-only cutover 资产")
    parser.add_argument("--bootstrap-root", type=Path, default=DEFAULT_BOOTSTRAP_ROOT)
    parser.add_argument("--bootstrap-only", action="store_true")
    args = parser.parse_args()
    findings = check_bootstrap(args.bootstrap_root)
    if not args.bootstrap_only:
        findings.extend(check_production_does_not_call_converter(REPO_ROOT))
        findings.extend(check_static_cutover(REPO_ROOT))
    print(json.dumps({"status": "PASS" if not findings else "FAIL", "findings": [asdict(item) for item in findings]}, ensure_ascii=False, indent=2))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
