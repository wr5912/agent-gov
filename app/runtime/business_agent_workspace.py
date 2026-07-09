from __future__ import annotations

import json
import os
from pathlib import Path

# 创建业务 Agent 时基于的模板 catalog（docker/runtime-volume-seeds/templates/business-agent/<template_id>/）。
# 默认按模块相对路径解析，容器内为 /app/docker/...（镜像 COPY），本机调试为 <repo>/docker/...；
# 可经 BUSINESS_AGENT_TEMPLATES_DIR 覆盖。
DEFAULT_TEMPLATE_ID = "general"
_TEMPLATES_DIR_DEFAULT = (
    Path(__file__).resolve().parents[2] / "docker" / "runtime-volume-seeds" / "templates" / "business-agent"
)
# 渲染占位符：模板文本里的 {{AGENT_ID}} / {{AGENT_NAME}} 被替换为具体值（双花括号不与 JSON 冲突）。
_PLACEHOLDER_AGENT_ID = "{{AGENT_ID}}"
_PLACEHOLDER_AGENT_NAME = "{{AGENT_NAME}}"

# general 模板缺失时的内联兜底（保证无种子目录的纯单测环境也能初始化）。
_STARTER_CLAUDE_MD = """# {name}

本工作区是 AgentGov 注册的业务 Agent `{agent_id}`（被治理对象）。

在此定义该 Agent 的角色、system prompt、技能与工具边界、行为约束；AgentGov 负责
其运行、反馈归因、评估和版本治理。高风险动作须经外部系统或授权用户确认。
"""

# 业务 Agent 是被治理对象：起始权限保守，默认只读自身工作区；Bash 由 sandbox、hook
# 与 deny 兜底直接执行，不走后端 HITL；写入工作区仍需确认。运行时治理根隔离由
# build_business_agent_profile 在 profile 层另行拒绝。
_STARTER_SETTINGS: dict = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "permissions": {
        "defaultMode": "default",
        "disableBypassPermissionsMode": "disable",
        "allow": ["Read(./**)", "Glob", "Grep", "Skill", "Bash(*)"],
        "ask": ["Edit(./**)", "Write(./**)"],
        "deny": ["Read(./.env)", "Read(./.env.*)", "Read(./secrets/**)"],
    },
}

# 起始 MCP 配置为空：不预置任何 server，更不预置 header/凭据；由用户按需添加。
_STARTER_MCP: dict = {"mcpServers": {}}


class UnknownBusinessAgentTemplate(ValueError):
    """请求的 template_id 不在 catalog 中（外部输入越权/拼写错误）。"""


def business_agent_templates_dir() -> Path:
    """模板 catalog 根目录（env 覆盖优先）。"""
    override = os.environ.get("BUSINESS_AGENT_TEMPLATES_DIR")
    return Path(override) if override else _TEMPLATES_DIR_DEFAULT


def list_business_agent_templates() -> list[str]:
    """列出可用 template_id（按名排序）；catalog 目录缺失时回退到内置 general。"""
    root = business_agent_templates_dir()
    if not root.is_dir():
        return [DEFAULT_TEMPLATE_ID]
    ids = sorted(p.name for p in root.iterdir() if p.is_dir())
    return ids or [DEFAULT_TEMPLATE_ID]


def _write_if_absent(path: Path, content: str) -> None:
    """仅在文件不存在时写入，保留用户对该业务 Agent 配置的编辑。"""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _render(text: str, *, agent_id: str, name: str) -> str:
    return text.replace(_PLACEHOLDER_AGENT_ID, agent_id).replace(_PLACEHOLDER_AGENT_NAME, name)


def _seed_inline_starter(workspace_dir: Path, *, agent_id: str, name: str) -> None:
    """general 模板目录不可用时的内联兜底（与历史起始内容一致）。"""
    _write_if_absent(workspace_dir / "CLAUDE.md", _STARTER_CLAUDE_MD.format(name=name, agent_id=agent_id))
    _write_if_absent(
        workspace_dir / ".claude" / "settings.json",
        json.dumps(_STARTER_SETTINGS, ensure_ascii=False, indent=2) + "\n",
    )
    _write_if_absent(workspace_dir / ".mcp.json", json.dumps(_STARTER_MCP, ensure_ascii=False, indent=2) + "\n")


def seed_business_agent_workspace(
    workspace_dir: Path,
    *,
    agent_id: str,
    name: str,
    template_id: str = DEFAULT_TEMPLATE_ID,
) -> str:
    """从 catalog 模板幂等播种业务 Agent workspace，渲染 {{AGENT_ID}}/{{AGENT_NAME}} 占位。

    - 未知 template_id 抛 UnknownBusinessAgentTemplate（由路由投影为 422）。
    - 已存在的文件不覆盖（保留用户编辑），FS 副作用幂等。
    - 模板内不含任何 api_key / MCP header / 本机私有路径。
    返回实际使用的 template_id。
    """
    template_id = (template_id or DEFAULT_TEMPLATE_ID).strip() or DEFAULT_TEMPLATE_ID
    workspace_dir.mkdir(parents=True, exist_ok=True)
    template_path = business_agent_templates_dir() / template_id

    if not template_path.is_dir():
        if template_id == DEFAULT_TEMPLATE_ID:
            _seed_inline_starter(workspace_dir, agent_id=agent_id, name=name)
            return template_id
        raise UnknownBusinessAgentTemplate(
            f"Unknown business agent template: {template_id!r}; available: {list_business_agent_templates()}"
        )

    for src in sorted(template_path.rglob("*")):
        if src.is_dir():
            continue
        rel = src.relative_to(template_path)
        if rel.name == "README.md":
            continue
        dest = workspace_dir / rel
        if dest.exists():
            continue
        rendered = _render(src.read_text(encoding="utf-8"), agent_id=agent_id, name=name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")
    return template_id


def initialize_business_agent_workspace(workspace_dir: Path, *, agent_id: str, name: str) -> None:
    """向后兼容入口：以默认 general 模板幂等初始化业务 Agent 工作区配置容器。"""
    seed_business_agent_workspace(workspace_dir, agent_id=agent_id, name=name, template_id=DEFAULT_TEMPLATE_ID)
