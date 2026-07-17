from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .agent_paths import InvalidAgentId, business_agent_layout, business_agents_root, validate_agent_id
from .business_agent_seed_catalog import declared_business_agent_ids
from .settings import AppSettings


def read_requires_web_hitl(workspace_dir: Path) -> bool:
    """从 Claude 原生 project settings 派生是否存在需要 Web HITL 的 ``ask`` 规则。"""
    path = workspace_dir / ".claude" / "settings.json"
    if not path.exists():
        return False
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(loaded, dict):
        return False
    permissions = loaded.get("permissions")
    if not isinstance(permissions, dict):
        return False
    ask = permissions.get("ask")
    return isinstance(ask, list) and any(isinstance(rule, str) and rule.strip() for rule in ask)


AgentRole = Literal[
    "main-agent",
    "business-agent",
    "governor",
]

# 动态注册业务 Agent 的通用角色（main-agent 是内置首个业务 Agent）。
BUSINESS_AGENT_ROLE = "business-agent"

# 业务 Agent 是被治理对象，治理 Agent 是闭环执行者（AGV-005）。
AgentCategory = Literal["business", "governance"]

# 治理 Agent 角色的单一真相来源；五个治理职责已合并为单一 governor（Issue #3）。
GOVERNANCE_AGENT_ROLES: frozenset[AgentRole] = frozenset({"governor"})


def agent_category(role: AgentRole) -> AgentCategory:
    """把 Agent 角色映射为业务/治理分类（AGV-005 结构化身份边界）。"""
    return "governance" if role in GOVERNANCE_AGENT_ROLES else "business"


MAIN_AGENT_PROFILE = "main-agent"
# 单一治理 Agent profile；归因/方案/执行/用例/回归影响按 job_type 复用同一执行者身份。
GOVERNOR_PROFILE = "governor"

PROFILE_VERSION_IDS: dict[AgentRole, str] = {
    "governor": "governor-v0.1.0",
}


@dataclass(frozen=True)
class AgentRuntimeProfile:
    name: str
    agent_id: str
    role: AgentRole
    workspace_dir: Path
    claude_root: Path
    claude_config_dir: Path
    data_dir: Path
    mcp_config_path: Path
    project_settings_path: Path
    langfuse_observation_name: str
    max_turns: int | None = None
    max_runtime_seconds: int = 300
    max_output_bytes: int = 2_000_000
    # 只读观测值，从 .claude/settings.json 的 permissions.ask 派生，不是第二份权限声明。
    requires_web_hitl: bool = False

    @property
    def category(self) -> AgentCategory:
        """业务 Agent（被治理对象）或治理 Agent（闭环执行者），由角色派生。"""
        return agent_category(self.role)


def agents_requiring_web_hitl(profiles: dict[str, AgentRuntimeProfile]) -> list[str]:
    """原生 project settings 含 ``ask`` 规则的 Agent id（排序）。"""
    return sorted(name for name, profile in profiles.items() if profile.requires_web_hitl)


def build_profiles(settings: AppSettings) -> dict[str, AgentRuntimeProfile]:
    return {
        # main 是预制的业务 Agent：与动态业务 Agent 同走 build_business_agent_profile，
        # workspace 落 data/business-agents/main-agent/workspace。governor 仍是特殊治理 Agent。
        MAIN_AGENT_PROFILE: build_business_agent_profile(settings, agent_id=MAIN_AGENT_PROFILE, workspace_dir=settings.main_workspace_dir),
        GOVERNOR_PROFILE: _governor_profile(settings),
    }


def discover_seeded_business_agents(settings: AppSettings) -> list[AgentRuntimeProfile]:
    """发现运行卷 ``data/business-agents/*`` 下已落盘（seed 预置或历史创建）的业务 Agent profile。

    每个直接子目录名即 ``agent_id``（与 ``build_business_agent_profile`` 的路径约定同源）；
    经 ``validate_agent_id`` 防目录穿越，非法名静默跳过，并要求其下存在 ``workspace/`` 才视为
    有效业务 Agent（过滤备份/残留等非 Agent 目录）。每个 Agent 的 ``workspace_dir`` 仍由
    ``business_agent_layout`` 这一单一真相派生，与运行时 profile 完全一致（返回的 ``profile.name``
    即 ``agent_id``，调用方据此归并）。

    用途：启动时把 seed 预置的多业务 Agent 幂等纳入注册表（main-agent 之外的 AAA/BBB…），
    使其与 main-agent 走同一注册/路由/治理抽象。main-agent 也会被发现，但与 ``build_profiles``
    的预制 main-agent 同 ``workspace_dir``，合并时幂等无冲突。

    语义：以磁盘为发现源——经 API 删除某 Agent 只移除注册表行、不清磁盘，故重启会重新发现登记。
    在“seed 声明基线业务 Agent”模型下这是预期行为；不在此引入 tombstone（超出本职责）。
    """
    root = business_agents_root(settings.data_dir)
    discovered: list[AgentRuntimeProfile] = []
    if root.is_symlink() or not root.is_dir():
        return discovered
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            agent_id = validate_agent_id(child.name)
        except InvalidAgentId:
            continue
        layout = business_agent_layout(settings.data_dir, agent_id)
        if layout.workspace.is_symlink() or not layout.workspace.is_dir():
            continue
        discovered.append(build_business_agent_profile(settings, agent_id=agent_id, workspace_dir=layout.workspace))
    return discovered


def seed_business_agent_ids() -> frozenset[str]:
    """声明式 seed 预置业务 Agent 的 agent_id 集合——docker/runtime-volume-seeds/data/business-agents/<id>/workspace。

    用于区分 seed（声明式基线，禁删）与用户创建（可 tombstone 删除）的业务 Agent（#26）。
    seed 目录是「出生配置」声明源，与运行卷的活配置无关（卷配置被反馈优化闭环修改、不可覆盖）。
    """
    ids: set[str] = set()
    for raw_agent_id in declared_business_agent_ids():
        try:
            ids.add(validate_agent_id(raw_agent_id))
        except InvalidAgentId:
            continue
    return frozenset(ids)


def candidate_profile(settings: AppSettings, *, agent_id: str, workspace_dir: Path, candidate_id: str) -> AgentRuntimeProfile:
    """候选版本 profile：cwd=候选 worktree，claude-root 隔离到 candidate-claude-roots/<id>，
    其余边界与该 Agent 的业务 profile 同构（不再 main 专属）。"""
    base = build_business_agent_profile(settings, agent_id=agent_id, workspace_dir=workspace_dir)
    claude_root = business_agent_layout(settings.data_dir, agent_id).version_base / "candidate-claude-roots" / candidate_id
    return AgentRuntimeProfile(
        name=f"{agent_id}-candidate",
        agent_id=agent_id,
        role=BUSINESS_AGENT_ROLE,
        workspace_dir=workspace_dir,
        claude_root=claude_root,
        claude_config_dir=claude_root / ".claude",
        data_dir=settings.data_dir,
        mcp_config_path=workspace_dir / ".mcp.json",
        project_settings_path=workspace_dir / ".claude" / "settings.json",
        langfuse_observation_name=f"runtime.candidate.{agent_id}",
        max_turns=base.max_turns,
        max_runtime_seconds=base.max_runtime_seconds,
        max_output_bytes=base.max_output_bytes,
    )


def build_business_agent_profile(settings: AppSettings, *, agent_id: str, workspace_dir: Path) -> AgentRuntimeProfile:
    """为一个注册业务 Agent 动态构造运行时 profile（AGV-004 运行态）。

    业务 Agent 是被治理对象；工具权限、路径边界、hooks 与 sandbox 由 workspace 的
    ``.claude/settings.json`` 原生声明。role 统一为 business-agent，name 为 agent_id。
    """
    claude_root = business_agent_layout(settings.data_dir, agent_id).claude_root
    return AgentRuntimeProfile(
        name=agent_id,
        agent_id=agent_id,
        role=BUSINESS_AGENT_ROLE,
        workspace_dir=workspace_dir,
        claude_root=claude_root,
        claude_config_dir=claude_root / ".claude",
        data_dir=settings.data_dir,
        mcp_config_path=workspace_dir / ".mcp.json",
        project_settings_path=workspace_dir / ".claude" / "settings.json",
        langfuse_observation_name=f"runtime.business_agent.{agent_id}",
        requires_web_hitl=read_requires_web_hitl(workspace_dir),
    )


def _governor_profile(settings: AppSettings) -> AgentRuntimeProfile:
    """单一治理 Agent profile：按 job_type 承担归因/方案/执行/用例/回归影响分析。

    它对业务 Agent 的读取范围与禁止写入规则均由 governor workspace 的项目设置声明；
    写业务配置只能走受治理 apply（隔离 worktree→operations→applier 护栏→change set→审批门）。
    各 job 的 prompt 与输出契约按 job_type 选择。
    """
    return AgentRuntimeProfile(
        **_readonly_feedback_kwargs(
            name=GOVERNOR_PROFILE,
            workspace_dir=settings.governor_workspace_dir,
            claude_root=settings.governor_claude_root,
            observation="runtime.governor",
            max_turns=16,
            settings=settings,
            max_runtime_seconds=settings.governance_agent_timeout_seconds,
        )
    )


def _readonly_feedback_kwargs(
    *,
    name: AgentRole,
    workspace_dir: Path,
    claude_root: Path,
    observation: str,
    max_turns: int | None,
    settings: AppSettings,
    max_runtime_seconds: int,
) -> dict[str, object]:
    return {
        "name": name,
        "agent_id": name,
        "role": name,
        "workspace_dir": workspace_dir,
        "claude_root": claude_root,
        "claude_config_dir": claude_root / ".claude",
        "data_dir": settings.data_dir,
        "mcp_config_path": workspace_dir / ".mcp.json",
        "project_settings_path": workspace_dir / ".claude" / "settings.json",
        "langfuse_observation_name": observation,
        "max_turns": max_turns,
        "max_runtime_seconds": max_runtime_seconds,
    }
