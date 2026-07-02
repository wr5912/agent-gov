"""业务 Agent 当前配置的 backend-owned grounding 读取器（供 governor 归因/优化 prompt）。

背景：governor 是只读运行时执行者，被 profile/hook/settings 三层禁读业务 Agent workspace；
它设计上只消费后端注入的 context。因此"让 governor 看到业务 Agent 配置"必须由后端**确定性读取**
配置资产并作为 grounding 注入 prompt，而不是给 governor 开读文件 skill。

只读**已提交、非敏感**的配置资产：CLAUDE.md（系统 prompt/角色）、.claude/settings.json 的权限、
.mcp.json 的 server 清单、以及 skills/agents 清单。绝不读 .env / *.local.* / secrets（本模块只显式请求
上述安全路径；file_context 另有 workspace 排除名单）。agent 不存在 / 文件缺失 / 超大均安全降级。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .agent_loader import discover_agents, discover_skills
from .agent_paths import InvalidAgentId, business_agent_layout, validate_agent_id
from .execution_targets import WorkspaceExecutionTargetPolicy
from .json_types import JsonObject
from .settings import AppSettings

_CLAUDE_MD = "CLAUDE.md"
_SETTINGS_JSON = ".claude/settings.json"
_MCP_JSON = ".mcp.json"


def build_business_agent_config_grounding(
    *,
    settings: AppSettings,
    agent_registry_store: Any,
    agent_id: str | None,
) -> JsonObject:
    """读业务 Agent 当前配置为 grounding dict；任何失败都安全降级为 workspace_present=False。"""
    grounding: JsonObject = {"agent_id": agent_id or "", "workspace_present": False}
    try:
        safe_id = validate_agent_id(agent_id)
    except InvalidAgentId:
        return grounding
    workspace = _resolve_workspace(settings, agent_registry_store, safe_id)
    if workspace is None or not workspace.is_dir():
        return grounding

    grounding["workspace_present"] = True
    policy = WorkspaceExecutionTargetPolicy(workspace)

    claude_md = _read_text(policy, _CLAUDE_MD)
    if claude_md is not None:
        grounding["claude_md"] = claude_md

    settings_permissions = _read_settings_permissions(policy)
    if settings_permissions:
        grounding["settings_permissions"] = settings_permissions

    mcp_servers = _read_mcp_servers(policy)
    if mcp_servers is not None:
        grounding["mcp_servers"] = mcp_servers

    grounding["skills"] = [_asset_entry(item) for item in discover_skills(workspace)]
    grounding["agents"] = [_asset_entry(item) for item in discover_agents(workspace)]
    return grounding


def _resolve_workspace(settings: AppSettings, agent_registry_store: Any, safe_id: str) -> Optional[Path]:
    record = None
    if agent_registry_store is not None:
        try:
            record = agent_registry_store.get_agent(safe_id)
        except Exception:  # noqa: BLE001 — registry 异常不应阻断 grounding
            record = None
    workspace_dir = getattr(record, "workspace_dir", "") if record is not None else ""
    if workspace_dir:
        return Path(str(workspace_dir))
    try:
        return business_agent_layout(Path(str(settings.data_dir)), safe_id).workspace
    except InvalidAgentId:
        return None


def _read_text(policy: WorkspaceExecutionTargetPolicy, rel_path: str) -> Optional[str]:
    context = policy.file_context(rel_path)
    text = context.get("content_text")
    return text if isinstance(text, str) and text.strip() else None


def _read_settings_permissions(policy: WorkspaceExecutionTargetPolicy) -> JsonObject:
    data = _read_json(policy, _SETTINGS_JSON)
    permissions = data.get("permissions") if isinstance(data, dict) else None
    if not isinstance(permissions, dict):
        return {}
    result: JsonObject = {}
    for key in ("allow", "ask", "deny"):
        value = permissions.get(key)
        if isinstance(value, list):
            result[key] = [str(item) for item in value]
    return result


def _read_mcp_servers(policy: WorkspaceExecutionTargetPolicy) -> Optional[list[str]]:
    text = _read_text(policy, _MCP_JSON)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return []
    return sorted(str(name) for name in servers)


def _read_json(policy: WorkspaceExecutionTargetPolicy, rel_path: str) -> Any:
    text = _read_text(policy, rel_path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _asset_entry(item: JsonObject) -> JsonObject:
    return {"name": item.get("name"), "description": item.get("description")}
