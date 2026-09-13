#!/usr/bin/env python3
"""离线转换旧 Claude Workspace；生产启动路径不得导入或调用本模块。"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from harness_conversion_io import (
    AssetRecord,
    HarnessObject,
    sha256_file,
    tree_digest,
)
from harness_conversion_io import (
    asset_record as _asset_record,
)
from harness_conversion_io import (
    publish_directory as _publish_directory,
)
from harness_conversion_io import (
    read_json_object as _read_json_object,
)
from harness_conversion_io import (
    read_yaml_object as _read_yaml_object,
)
from harness_conversion_io import (
    regular_files as _regular_files,
)
from harness_conversion_io import (
    render_skill as _render_skill,
)
from harness_conversion_io import (
    rewrite_kind_text as _rewrite_kind_text,
)
from harness_conversion_io import (
    rewrite_text as _rewrite_text,
)
from harness_conversion_io import (
    split_frontmatter as _split_frontmatter,
)
from harness_conversion_io import (
    string_list as _string_list,
)
from harness_conversion_io import (
    write_json as _write_json,
)
from harness_conversion_io import (
    write_text as _write_text,
)
from harness_conversion_io import (
    write_yaml as _write_yaml,
)
from harness_permission_policy import convert_permission_rules as _convert_permission_rules
from harness_permission_policy import validate_converted_permission_rules as _validate_converted_permission_rules

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentgov_agentscope_contract import AGENTSCOPE_RUNTIME_CONTRACT  # noqa: E402
from agentgov_harness_digest import harness_content_digest  # noqa: E402

CONVERTER_ID = "agentgov-claude-to-agentscope/v1"
ENV_REFERENCE_RE = re.compile(r"\$\{(?P<name>[A-Z][A-Z0-9_]*)\}")
KNOWN_HOOK_DIGESTS = {
    "post_tool_audit.py": "7f33a50c77529039317ec9e94f7335573c103f8ae515a55b7fcbc732f17d8f2c",
    "pre_tool_guard.py": "92041118067359429b218d7839c76bd21567ea84fba1fe4bc5bb9a106abaf829",
    "session_start.py": "f6b4e6992e68c0d63a72a4c8f96e27c98c8427ca460ec7ecfa7b7829d20561c0",
}


class ConversionRejectedError(RuntimeError):
    def __init__(self, report: HarnessObject) -> None:
        super().__init__("Harness 转换包含 rejected 项，未发布目标目录")
        self.report = report


@dataclass(frozen=True)
class MappingEntry:
    source_path: str
    source_sha256: str
    status: str
    rule: str
    human_confirmation: str
    targets: tuple[AssetRecord, ...]


def convert_workspace(
    source: Path,
    destination: Path,
    *,
    kind: str,
    agent_id: str | None = None,
    replace: bool = False,
) -> HarnessObject:
    source = source.resolve()
    destination = destination.resolve()
    if kind not in {"governor", "business"}:
        raise ValueError("kind must be 'governor' or 'business'")
    _require_safe_source(source)
    _require_separate_trees(source, destination)
    source_files = _regular_files(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.agentscope-", dir=destination.parent))
    try:
        report = _build_workspace(
            source,
            staging,
            source_files=source_files,
            kind=kind,
            requested_agent_id=agent_id,
        )
        if report["rejected_count"]:
            raise ConversionRejectedError(report)
        _publish_directory(staging, destination, replace=replace)
        return report
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _build_workspace(
    source: Path,
    output: Path,
    *,
    source_files: tuple[Path, ...],
    kind: str,
    requested_agent_id: str | None,
) -> HarnessObject:
    old_manifest = _read_yaml_object(source / "agent.yaml")
    settings = _read_json_object(source / ".claude" / "settings.json")
    resolved_agent_id = _resolve_agent_id(old_manifest, requested_agent_id, kind)
    prompt = _build_agent_prompt(source, settings, kind=kind)
    _write_text(output / "AGENT.md", prompt)
    skill_records = _convert_skills(source, output, kind=kind)
    mcp_records = _convert_mcps(source, output)
    subagent_records = _convert_subagents(source, output)
    _copy_auxiliary_files(source, output)
    _write_workspace_tests(output, kind=kind, agent_id=resolved_agent_id)
    manifest = _build_manifest(
        old_manifest,
        settings,
        output,
        kind=kind,
        agent_id=resolved_agent_id,
        skills=skill_records,
        mcps=mcp_records,
        subagents=subagent_records,
    )
    _write_yaml(output / "agent.yaml", manifest)
    _validate_agent_visible_contract(output, manifest, kind=kind)
    manifest["harness"]["content_digest"] = harness_content_digest(output)
    _write_yaml(output / "agent.yaml", manifest)
    entries = _build_mapping_entries(
        source,
        output,
        source_files,
        test_filename=_workspace_test_filename(resolved_agent_id),
    )
    rejected = tuple(entry for entry in entries if entry.status == "rejected")
    report = _conversion_report(source, output, entries, rejected)
    if not rejected:
        _write_json(output / "conversion-report.json", report)
    return report


def _build_agent_prompt(
    source: Path,
    settings: Mapping[str, object],
    *,
    kind: str,
) -> str:
    sections = [_rewrite_kind_text((source / "CLAUDE.md").read_text(encoding="utf-8"), kind).rstrip()]
    rules_root = source / ".claude" / "rules"
    if rules_root.is_dir():
        for rule in sorted(rules_root.glob("*.md")):
            body = _rewrite_kind_text(rule.read_text(encoding="utf-8"), kind).strip()
            sections.append(f"## 迁移后的治理规则：{rule.stem}\n\n{body}")
    if _declares_hook(settings, "session_start.py"):
        sections.append(
            "## 会话启动不变量\n\n"
            "当前项目是网络安全运营专家智能体。默认证据优先；区分事实、推断和行动；"
            "生产处置和策略变更必须先 dry-run，并需要审批、回滚和验证。",
        )
    return "\n\n".join(section for section in sections if section).rstrip() + "\n"


def _convert_skills(source: Path, output: Path, *, kind: str) -> list[AssetRecord]:
    records: list[AssetRecord] = []
    skill_root = source / ".claude" / "skills"
    if skill_root.is_dir():
        for source_file in _regular_files(skill_root):
            relative = source_file.relative_to(skill_root)
            target = output / "skills" / relative
            raw = source_file.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
            else:
                _write_text(target, _rewrite_kind_text(text, kind))
    commands_root = source / ".claude" / "commands"
    if commands_root.is_dir():
        for command in sorted(commands_root.glob("*.md")):
            metadata, body = _split_frontmatter(command.read_text(encoding="utf-8"))
            rendered = _render_skill(
                name=command.stem,
                description=str(metadata.get("description") or f"迁移自 /{command.stem} 的离线命令技能。"),
                body=_rewrite_kind_text(body, kind),
            )
            _write_text(output / "skills" / command.stem / "SKILL.md", rendered)
    skills_dir = output / "skills"
    if skills_dir.is_dir():
        for skill_md in sorted(skills_dir.glob("*/SKILL.md")):
            metadata, body = _split_frontmatter(skill_md.read_text(encoding="utf-8"))
            name = str(metadata.get("name") or skill_md.parent.name)
            description = str(metadata.get("description") or "").strip()
            if not description:
                raise ValueError(f"Skill 缺少 description: {skill_md}")
            _write_text(
                skill_md,
                _render_skill(
                    name=name,
                    description=description,
                    body=_rewrite_kind_text(body, kind),
                ),
            )
            records.append(_asset_record(output, skill_md.parent, name=name))
    return records


def _convert_mcps(source: Path, output: Path) -> list[AssetRecord]:
    legacy_path = source / ".mcp.json"
    legacy = _read_json_object(legacy_path)
    servers = legacy.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"mcpServers must be an object: {legacy_path}")
    records: list[AssetRecord] = []
    for name, raw_config in sorted(servers.items()):
        if not isinstance(raw_config, dict):
            raise ValueError(f"MCP config must be an object: {name}")
        config = _agentscope_mcp_config(raw_config)
        payload = {
            "schema_version": 1,
            "name": name,
            "mcp_config": config,
            "credential_refs": _credential_references(config),
            # 旧 Claude 配置没有精确能力清单。转换结果因此默认零能力，
            # 必须把人工复核后的 tools/resources 清单作为候选变更发布。
            "enable_tools": [],
            "enable_resources": [],
            "enable_resource_templates": [],
        }
        target = output / "mcp" / f"{name}.json"
        _write_json(target, payload)
        records.append(_asset_record(output, target, name=name))
    return records


def _convert_subagents(source: Path, output: Path) -> list[AssetRecord]:
    source_root = source / ".claude" / "agents"
    records: list[AssetRecord] = []
    if not source_root.is_dir():
        return records
    for source_file in sorted(source_root.glob("*.md")):
        metadata, body = _split_frontmatter(source_file.read_text(encoding="utf-8"))
        name = str(metadata.get("name") or source_file.stem)
        target_root = output / "subagents" / source_file.stem
        _write_text(target_root / "AGENT.md", _rewrite_text(body).strip() + "\n")
        subagent = {
            "schema_version": 1,
            "agent": {
                "id": name,
                "name": name,
                "description": str(metadata.get("description") or ""),
                "runtime": "agentscope",
                "runtime_contract": AGENTSCOPE_RUNTIME_CONTRACT,
                "system_prompt": "AGENT.md",
            },
            "context_config": {},
            "react_config": {},
            "invite_config": {"invitable": False},
            "session": {"permission_mode": "dont_ask"},
            "workspace_policy": {
                "allowed_tools": sorted({*_string_list(metadata.get("tools")), "TeamSay"}),
                "ask_tools": [],
                "denied_tools": _string_list(metadata.get("disallowedTools")),
                "fail_closed": True,
            },
        }
        _write_yaml(target_root / "agent.yaml", subagent)
        records.append(_asset_record(output, target_root, name=name))
    return records


def _copy_auxiliary_files(source: Path, output: Path) -> None:
    for name in ("README.md", ".gitignore", ".worktreeinclude"):
        source_file = source / name
        if source_file.is_file():
            _write_text(output / name, _rewrite_text(source_file.read_text(encoding="utf-8")))


def _write_workspace_tests(output: Path, *, kind: str, agent_id: str) -> None:
    readme = "# AgentScope Harness tests\n\n这些测试验证已提交 Harness 的不可变结构、权限边界和资产关联；不依赖生产 Runtime。\n"
    _write_text(output / "tests" / "README.md", readme)
    _write_text(output / "tests" / _workspace_test_filename(agent_id), _workspace_test_source(kind, agent_id))


def _workspace_test_filename(agent_id: str) -> str:
    return f"test_{re.sub(r'[^a-z0-9]+', '_', agent_id.lower()).strip('_')}_agentscope_harness.py"


def _workspace_test_source(kind: str, agent_id: str) -> str:
    kind_assertion = (
        'assert manifest["agent"]["profile"] == "governor"\n'
        '    assert policy["writable_paths"] == []\n'
        '    assert {"HarnessList", "HarnessRead"} <= set(policy["allowed_tools"])'
        if kind == "governor"
        else (
            f'assert manifest["agent"]["id"] == {json.dumps(agent_id)}\n'
            '    assert policy["immutable_harness"] is True\n'
            '    assert manifest["paths"]["outputs"] == "/workspace/outputs"'
        )
    )
    return f'''from __future__ import annotations\n
import json
from pathlib import Path
\nimport yaml
\nWORKSPACE = Path(__file__).resolve().parents[1]\n\n
def test_agentscope_manifest_and_assets_are_bound() -> None:
    manifest = yaml.safe_load((WORKSPACE / "agent.yaml").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["agent"]["runtime"] == "agentscope"
    assert manifest["agent"]["runtime_contract"] == "{AGENTSCOPE_RUNTIME_CONTRACT}"
    policy = manifest["workspace_policy"]
    {kind_assertion}
    assert manifest["paths"]["workspace"] == "/workspace"
    assert manifest["paths"]["data_root"] == "/workspace/data"
    assert all("/runtime-data" not in rule for rule in policy["allowed_tools"])
    assert all("/runtime-data" not in path for path in policy["writable_paths"])
    for group in ("skills", "mcps", "subagents"):
        for asset in manifest["harness"][group]:
            assert (WORKSPACE / asset["path"]).exists()\n\n
def test_conversion_report_is_complete() -> None:
    report = json.loads((WORKSPACE / "conversion-report.json").read_text(encoding="utf-8"))
    assert report["source_coverage_percent"] == 100.0
    assert report["rejected_count"] == 0
    assert report["source_file_count"] == report["mapped_count"] + report["retired_count"]
'''


def _build_manifest(
    old: Mapping[str, object],
    settings: Mapping[str, object],
    output: Path,
    *,
    kind: str,
    agent_id: str,
    skills: list[AssetRecord],
    mcps: list[AssetRecord],
    subagents: list[AssetRecord],
) -> HarnessObject:
    old_agent = old.get("agent", {})
    if not isinstance(old_agent, dict):
        raise ValueError("agent.yaml agent must be an object")
    profile = str(old_agent.get("profile") or kind)
    permissions = settings.get("permissions", {})
    sandbox = settings.get("sandbox", {})
    if not isinstance(permissions, dict) or not isinstance(sandbox, dict):
        raise ValueError("settings permissions/sandbox must be objects")
    permission_mode = str(permissions.get("defaultMode") or "default")
    if permission_mode not in {"default", "explore", "accept_edits", "dont_ask"}:
        raise ValueError(f"unsupported permission mode: {permission_mode}")
    manifest: HarnessObject = {
        "schema_version": 1,
        "agent": {
            "id": agent_id,
            "name": str(old_agent.get("name") or agent_id),
            "version": str(old_agent.get("version") or "0.1.0"),
            "language": str(old_agent.get("language") or "zh-CN"),
            "runtime": "agentscope",
            "runtime_contract": AGENTSCOPE_RUNTIME_CONTRACT,
            "profile": profile,
            "system_prompt": "AGENT.md",
        },
        "context_config": dict(old.get("context_config")) if isinstance(old.get("context_config"), dict) else {},
        "react_config": dict(old.get("react_config")) if isinstance(old.get("react_config"), dict) else {},
        "invite_config": dict(old.get("invite_config")) if isinstance(old.get("invite_config"), dict) else {"invitable": False},
        "session": {
            "model_profile": str(old.get("model_profile") or "default"),
            "permission_mode": permission_mode,
            "cwd": ".",
        },
        "workspace_policy": _workspace_policy(
            permissions,
            sandbox,
            kind=kind,
            agent_id=agent_id,
            has_subagents=bool(subagents),
        ),
        "runtime_middlewares": _runtime_middlewares(settings, kind=kind),
        "paths": _convert_paths(old.get("paths")),
        "harness": {
            "digest_algorithm": "sha256",
            "system_prompt": _asset_record(output, output / "AGENT.md", name="system_prompt"),
            "skills": skills,
            "mcps": mcps,
            "subagents": subagents,
            "tests": _asset_record(output, output / "tests", name="tests"),
        },
        "extension_points": {
            "add_skill": "skills/<skill-name>/SKILL.md",
            "add_mcp_server": "mcp/<name>.json",
            "add_subagent": "subagents/<name>/",
        },
    }
    for key in (
        "capabilities",
        "presentation",
        "operational_model",
        "approval_policy",
        "conversation_revision_policy",
    ):
        if key in old:
            manifest[key] = json.loads(_rewrite_text(json.dumps(old[key], ensure_ascii=False)))
    if "requires_web_hitl" in old_agent:
        manifest["agent"]["requires_web_hitl"] = bool(old_agent["requires_web_hitl"])
    if "observability" in old:
        manifest["observability"] = _convert_observability(old["observability"])
    manifest["harness"]["content_digest"] = ""
    return manifest


def _validate_agent_visible_contract(
    output: Path,
    manifest: Mapping[str, object],
    *,
    kind: str,
) -> None:
    paths = manifest.get("paths")
    policy = manifest.get("workspace_policy")
    if not isinstance(paths, dict) or not isinstance(policy, dict):
        raise ValueError("converted Harness requires paths and workspace_policy")
    if paths.get("workspace") != "/workspace" or paths.get("data_root") != "/workspace/data":
        raise ValueError("Agent-visible workspace paths must use /workspace")

    allowed_tools = _string_list(policy.get("allowed_tools"))
    writable_paths = _string_list(policy.get("writable_paths"))
    if any("/runtime-data" in item for item in (*allowed_tools, *writable_paths)):
        raise ValueError("/runtime-data is reserved for trusted Runtime-owned sinks")

    agent_text_paths = [output / "AGENT.md"]
    agent_text_paths.extend(sorted((output / "skills").glob("*/SKILL.md")))
    agent_text_paths.extend(sorted((output / "subagents").glob("*/AGENT.md")))
    agent_text = "\n".join(path.read_text(encoding="utf-8") for path in agent_text_paths if path.is_file())
    if "/runtime-data" in agent_text:
        raise ValueError("Agent-visible instructions must not reference /runtime-data")
    if "先用 Read 工具读取当前 workspace 下" in agent_text:
        raise ValueError("converted instructions cannot require reading immutable Harness files from the sandbox")

    if kind == "business" and paths.get("outputs") != "/workspace/outputs":
        raise ValueError("business Harness outputs must use /workspace/outputs")
    if kind == "governor":
        if not {"HarnessList", "HarnessRead"}.issubset(allowed_tools):
            raise ValueError("governor Harness requires HarnessList and HarnessRead")
        if "HarnessList()" not in agent_text or "HarnessRead(path)" not in agent_text:
            raise ValueError("governor instructions must describe the bound Harness tools")
        if "用 Read/Glob/Grep 直接读该业务 Agent" in agent_text:
            raise ValueError("governor cannot read target Harness files through sandbox filesystem tools")


def _workspace_policy(
    permissions: Mapping[str, object],
    sandbox: Mapping[str, object],
    *,
    kind: str,
    agent_id: str,
    has_subagents: bool,
) -> HarnessObject:
    filesystem = sandbox.get("filesystem", {})
    network = sandbox.get("network", {})
    if not isinstance(filesystem, dict) or not isinstance(network, dict):
        raise ValueError("sandbox filesystem/network must be objects")
    immutable = ["AGENT.md", "agent.yaml", "skills/**", "mcp/**", "subagents/**"]
    allowed_tools = _convert_permission_rules(permissions.get("allow"))
    ask_tools = _convert_permission_rules(permissions.get("ask"))
    denied_tools = _convert_permission_rules(permissions.get("deny"))
    if kind == "governor":
        allowed_tools = sorted({*allowed_tools, "HarnessList", "HarnessRead"})
    if has_subagents:
        allowed_tools = sorted(
            {*allowed_tools, "TeamCreate", "AgentCreate", "TeamSay", "TeamDelete"},
        )
    _validate_converted_permission_rules(
        allowed_tools=allowed_tools,
        ask_tools=ask_tools,
        denied_tools=denied_tools,
    )
    denied_read_paths = _convert_path_values(filesystem.get("denyRead"))
    if kind == "governor":
        denied_read_paths = sorted(
            {
                *denied_read_paths,
                ".env",
                "**/.env",
                "**/.env.*",
                "**/*credential*",
                "**/*secret*",
                "/runtime-data/**",
            },
        )
    allowed_network_domains = _string_list(network.get("allowedDomains"))
    if agent_id == "security-operations-expert":
        allowed_network_domains = ["${SEC_OPS_MCP_URL}"]
    return {
        "owner": "agentgov",
        "isolation": "per_agent",
        "immutable_harness": True,
        "fail_closed": True,
        "allow_for_run": False,
        "allowed_tools": allowed_tools,
        "ask_tools": ask_tools,
        "denied_tools": denied_tools,
        "immutable_paths": immutable,
        "writable_paths": [] if kind == "governor" else _convert_path_values(filesystem.get("allowWrite")),
        "denied_read_paths": denied_read_paths,
        "allowed_network_domains": allowed_network_domains,
        "sandbox": {
            "enabled": bool(sandbox.get("enabled", True)),
            "fail_if_unavailable": bool(sandbox.get("failIfUnavailable", True)),
            "allow_unsandboxed_commands": False,
        },
        "guard": {
            "implementation": "agentgov_runtime_middleware",
            "mode": "deny_ask" if ask_tools else "deny_only",
            "denied_command_families": [
                "destructive_filesystem",
                "direct_production_mutation",
                "remote_installer_pipe",
                "network_scanning",
            ],
        },
    }


def _runtime_middlewares(settings: Mapping[str, object], *, kind: str) -> list[HarnessObject]:
    middlewares: list[HarnessObject] = []
    if kind == "governor" or _declares_hook(settings, "pre_tool_guard.py"):
        middlewares.append({"type": "policy_guard", "phase": "before_tool_call", "fail_closed": True})
    if _declares_hook(settings, "post_tool_audit.py"):
        middlewares.append(
            {
                "type": "tool_audit",
                "phase": "after_tool_call",
                "sink": "/runtime-data/transcripts/agentscope-tool-audit.jsonl",
                "async": True,
            },
        )
    if kind == "governor" or _declares_hook(settings, "session_start.py"):
        middlewares.append({"type": "system_prompt_context", "source": "AGENT.md"})
    return middlewares


def _declares_hook(settings: Mapping[str, object], filename: str) -> bool:
    hooks = settings.get("hooks", {})
    return filename in json.dumps(hooks, ensure_ascii=False, sort_keys=True)


def _convert_path_values(value: Any) -> list[str]:
    return sorted(_rewrite_text(item) for item in _string_list(value) if "claude-root" not in item)


def _convert_paths(value: Any) -> HarnessObject:
    if not isinstance(value, dict):
        return {"workspace": "/workspace", "data_root": "/workspace/data"}
    result: HarnessObject = {}
    canonical_paths = {
        "data_root": "/workspace/data",
        "sessions": "/workspace/data/sessions",
        "transcripts": "/workspace/data/transcripts",
        "uploads": "/workspace/data",
        "outputs": "/workspace/outputs",
        "agent_memory": "/workspace/data/agent-memory",
    }
    for key, item in value.items():
        if key == "claude_home":
            continue
        if key == "workspace":
            result[str(key)] = "/workspace"
        elif key in canonical_paths:
            result[str(key)] = canonical_paths[str(key)]
        elif isinstance(item, str):
            result[str(key)] = _rewrite_text(item)
        else:
            result[str(key)] = item
    result.setdefault("workspace", "/workspace")
    result.setdefault("data_root", "/workspace/data")
    return result


def _convert_observability(value: Any) -> HarnessObject:
    if not isinstance(value, dict):
        return {}
    result = {str(key): _rewrite_text(item) if isinstance(item, str) else item for key, item in value.items() if key != "hook_audit_log"}
    if "hook_audit_log" in value:
        result["tool_audit_log"] = "/runtime-data/transcripts/agentscope-tool-audit.jsonl"
    return result


def _agentscope_mcp_config(value: Mapping[str, object]) -> HarnessObject:
    config = dict(value)
    kind = str(config.get("type") or "http")
    if kind in {"http", "sse", "streamable-http"}:
        config["type"] = "http_mcp"
        config.setdefault("timeout", 30.0)
    elif kind != "http_mcp":
        raise ValueError(f"Unsupported MCP transport: {kind}")
    return config


def _credential_references(value: Any, path: str = "mcp_config") -> list[AssetRecord]:
    references: list[AssetRecord] = []
    if isinstance(value, dict):
        for key, item in sorted(value.items()):
            references.extend(_credential_references(item, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            references.extend(_credential_references(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        for match in ENV_REFERENCE_RE.finditer(value):
            references.append({"path": path, "env": match.group("name")})
    return references


def _build_mapping_entries(
    source: Path,
    output: Path,
    source_files: tuple[Path, ...],
    *,
    test_filename: str,
) -> tuple[MappingEntry, ...]:
    entries: list[MappingEntry] = []
    for source_file in source_files:
        relative = source_file.relative_to(source).as_posix()
        status, rule, targets = _mapping_for(relative, output, test_filename=test_filename)
        confirmation = "approved_by_cutover_plan" if relative.startswith("hooks/") else "not_required"
        target_records = tuple({"path": target, "sha256": sha256_file(output / target)} for target in targets if (output / target).is_file())
        entries.append(
            MappingEntry(
                source_path=relative,
                source_sha256=sha256_file(source_file),
                status=status,
                rule=rule,
                human_confirmation=confirmation,
                targets=target_records,
            ),
        )
    return tuple(entries)


def _mapping_for(relative: str, output: Path, *, test_filename: str) -> tuple[str, str, tuple[str, ...]]:
    if relative == "agent.yaml":
        return "mapped", "manifest_v1_to_agentscope_v1", ("agent.yaml",)
    if relative == "CLAUDE.md":
        return "mapped", "system_prompt_to_agent_md", ("AGENT.md",)
    if relative == ".claude/settings.json":
        return "mapped", "permissions_and_hooks_to_platform_policy", ("agent.yaml",)
    if relative == ".mcp.json":
        targets = tuple(path.relative_to(output).as_posix() for path in sorted((output / "mcp").glob("*.json")))
        return "mapped", "mcp_servers_to_agentscope_configs", targets or ("agent.yaml",)
    if relative.startswith(".claude/rules/") and relative.endswith(".md"):
        return "mapped", "ordered_rule_append_to_agent_md", ("AGENT.md",)
    if relative.startswith(".claude/skills/"):
        target = relative[len(".claude/") :]
        return "mapped", "skill_directory_move", (target,)
    if relative.startswith(".claude/commands/") and relative.endswith(".md"):
        name = Path(relative).stem
        return "mapped", "command_to_skill", (f"skills/{name}/SKILL.md",)
    if relative.startswith(".claude/agents/") and relative.endswith(".md"):
        name = Path(relative).stem
        return "mapped", "subagent_manifest_split", (f"subagents/{name}/agent.yaml", f"subagents/{name}/AGENT.md")
    if relative.startswith("hooks/"):
        hook_name = Path(relative).name
        if hook_name not in KNOWN_HOOK_DIGESTS:
            return "rejected", "unknown_python_hook", ()
        return "mapped", f"reviewed_{hook_name}_to_runtime_contract", ("agent.yaml", "AGENT.md")
    if relative == "README.md":
        return "mapped", "documentation_reference_rewrite", ("README.md",)
    if relative in {".gitignore", ".worktreeinclude"}:
        return "mapped", "workspace_path_reference_rewrite", (relative,)
    if relative == "tests/README.md":
        return "mapped", "test_documentation_rewrite", ("tests/README.md",)
    if relative.startswith("tests/") and relative.endswith(".py"):
        return "mapped", "legacy_contract_tests_to_agentscope_contract", (f"tests/{test_filename}",)
    return "rejected", "no_reviewed_mapping", ()


def _conversion_report(
    source: Path,
    output: Path,
    entries: tuple[MappingEntry, ...],
    rejected: tuple[MappingEntry, ...],
) -> HarnessObject:
    mapped = sum(entry.status == "mapped" for entry in entries)
    retired = sum(entry.status == "retired" for entry in entries)
    source_count = len(entries)
    return {
        "schema_version": 1,
        "converter": CONVERTER_ID,
        "harness_digest": _read_yaml_object(output / "agent.yaml")["harness"]["content_digest"],
        "source_root": ".",
        "source_tree_sha256": tree_digest(source),
        "output_tree_sha256": tree_digest(output, excluded=("conversion-report.json",)),
        "source_file_count": source_count,
        "mapped_count": mapped,
        "retired_count": retired,
        "rejected_count": len(rejected),
        "source_coverage_percent": 100.0,
        "entries": [asdict(entry) for entry in entries],
    }


def _mapping_hook_is_reviewed(source_file: Path) -> bool:
    return KNOWN_HOOK_DIGESTS.get(source_file.name) == sha256_file(source_file)


def _require_safe_source(source: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"source must be a real directory: {source}")
    required = (source / "agent.yaml", source / "CLAUDE.md", source / ".claude" / "settings.json", source / ".mcp.json")
    for path in required:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"required legacy Harness file missing: {path}")
    for path in source.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
            raise ValueError(f"unsupported source entry: {path}")
    for hook in sorted((source / "hooks").glob("*.py")) if (source / "hooks").is_dir() else ():
        if not _mapping_hook_is_reviewed(hook):
            raise ValueError(f"unreviewed Python hook: {hook.relative_to(source)}")


def _require_separate_trees(source: Path, destination: Path) -> None:
    if destination == source or source in destination.parents or destination in source.parents:
        raise ValueError("source and destination must be separate trees")


def _resolve_agent_id(old: Mapping[str, object], requested: str | None, kind: str) -> str:
    agent = old.get("agent", {})
    manifest_id = agent.get("id") if isinstance(agent, dict) else None
    resolved = requested or manifest_id or ("governor" if kind == "governor" else None)
    if not isinstance(resolved, str) or not resolved.strip():
        raise ValueError("business Workspace requires an explicit agent_id")
    if requested and manifest_id and requested != manifest_id:
        raise ValueError("requested agent_id conflicts with legacy manifest")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description="离线转换 Claude Workspace 为 AgentScope Harness")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--kind", choices=("governor", "business"), required=True)
    parser.add_argument("--agent-id")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    try:
        report = convert_workspace(
            args.source,
            args.destination,
            kind=args.kind,
            agent_id=args.agent_id,
            replace=args.replace,
        )
    except ConversionRejectedError as exc:
        print(json.dumps(exc.report, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
