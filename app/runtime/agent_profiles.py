from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .agent_paths import InvalidAgentId, business_agent_layout, business_agents_root, validate_agent_id
from .settings import AppSettings

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
    role: AgentRole
    workspace_dir: Path
    claude_root: Path
    claude_config_dir: Path
    data_dir: Path
    mcp_config_path: Path
    project_settings_path: Path
    langfuse_observation_name: str
    readable_paths: tuple[Path, ...]
    writable_paths: tuple[Path, ...]
    denied_paths: tuple[Path, ...]
    max_turns: int | None = None
    max_runtime_seconds: int = 300
    max_output_bytes: int = 2_000_000

    @property
    def category(self) -> AgentCategory:
        """业务 Agent（被治理对象）或治理 Agent（闭环执行者），由角色派生。"""
        return agent_category(self.role)


def build_profiles(settings: AppSettings) -> dict[str, AgentRuntimeProfile]:
    return {
        # main 是预制的业务 Agent：与动态业务 Agent 同走 build_business_agent_profile，
        # workspace 落 data/business-agents/main-agent/workspace。governor 仍是特殊治理 Agent。
        MAIN_AGENT_PROFILE: build_business_agent_profile(
            settings, agent_id=MAIN_AGENT_PROFILE, workspace_dir=settings.main_workspace_dir
        ),
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
    if not root.is_dir():
        return discovered
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        try:
            agent_id = validate_agent_id(child.name)
        except InvalidAgentId:
            continue
        layout = business_agent_layout(settings.data_dir, agent_id)
        if not layout.workspace.is_dir():
            continue
        discovered.append(
            build_business_agent_profile(settings, agent_id=agent_id, workspace_dir=layout.workspace)
        )
    return discovered


def seed_business_agent_ids() -> frozenset[str]:
    """声明式 seed 预置业务 Agent 的 agent_id 集合——docker/runtime-volume-seeds/data/business-agents/<id>/workspace。

    用于区分 seed（声明式基线，禁删）与用户创建（可 tombstone 删除）的业务 Agent（#26）。
    seed 目录是「出生配置」声明源，与运行卷的活配置无关（卷配置被反馈优化闭环修改、不可覆盖）。
    """
    seed_root = Path(__file__).resolve().parents[2] / "docker" / "runtime-volume-seeds" / "data" / "business-agents"
    if not seed_root.is_dir():
        return frozenset()
    ids: set[str] = set()
    for child in sorted(seed_root.iterdir()):
        if not child.is_dir() or not (child / "workspace").is_dir():
            continue
        try:
            ids.add(validate_agent_id(child.name))
        except InvalidAgentId:
            continue
    return frozenset(ids)


def candidate_profile(
    settings: AppSettings, *, agent_id: str, workspace_dir: Path, candidate_id: str
) -> AgentRuntimeProfile:
    """候选版本 profile：cwd=候选 worktree，claude-root 隔离到 candidate-claude-roots/<id>，
    其余边界与该 Agent 的业务 profile 同构（不再 main 专属）。"""
    base = build_business_agent_profile(settings, agent_id=agent_id, workspace_dir=workspace_dir)
    claude_root = settings.data_dir / "agent-governance" / "candidate-claude-roots" / candidate_id
    return AgentRuntimeProfile(
        name=f"{agent_id}-candidate",
        role=BUSINESS_AGENT_ROLE,
        workspace_dir=workspace_dir,
        claude_root=claude_root,
        claude_config_dir=claude_root / ".claude",
        data_dir=settings.data_dir,
        mcp_config_path=workspace_dir / ".mcp.json",
        project_settings_path=workspace_dir / ".claude" / "settings.json",
        langfuse_observation_name=f"runtime.candidate.{agent_id}",
        readable_paths=(workspace_dir, settings.data_dir),
        writable_paths=base.writable_paths,
        denied_paths=(settings.governor_claude_root, claude_root),
        max_turns=base.max_turns,
        max_runtime_seconds=base.max_runtime_seconds,
        max_output_bytes=base.max_output_bytes,
    )


def build_business_agent_profile(settings: AppSettings, *, agent_id: str, workspace_dir: Path) -> AgentRuntimeProfile:
    """为一个注册业务 Agent 动态构造运行时 profile（AGV-004 运行态）。

    业务 Agent 是被治理对象：可读自身 workspace 与 data_dir、可写输出目录，
    但不得写入任何治理 Agent 根目录。role 统一为 business-agent，name 为 agent_id。
    """
    claude_root = business_agent_layout(settings.data_dir, agent_id).claude_root
    return AgentRuntimeProfile(
        name=agent_id,
        role=BUSINESS_AGENT_ROLE,
        workspace_dir=workspace_dir,
        claude_root=claude_root,
        claude_config_dir=claude_root / ".claude",
        data_dir=settings.data_dir,
        mcp_config_path=workspace_dir / ".mcp.json",
        project_settings_path=workspace_dir / ".claude" / "settings.json",
        langfuse_observation_name=f"runtime.business_agent.{agent_id}",
        readable_paths=(workspace_dir, settings.data_dir),
        writable_paths=(settings.data_dir / "outputs",),
        # 业务 Agent 不得自读自身 SDK 运行态家目录（claude-root 在 data_dir 下、且可能嵌于 cwd），
        # 否则可经 Read(./claude-root/**) 读到 session/缓存/凭据态。denied 在 policy.py 优先于 readable。
        denied_paths=(settings.governor_claude_root, claude_root),
    )


def _governor_profile(settings: AppSettings) -> AgentRuntimeProfile:
    """单一治理 Agent profile：按 job_type 承担归因/方案/执行/用例/回归影响分析。

    它是只读闭环执行者：不持有可写工作区，输出经后端投影；不得读写 main workspace
    与 main claude_root（denied_paths）。各 job 的 prompt 与输出契约仍按 job_type 选择。
    """
    return AgentRuntimeProfile(
        **_readonly_feedback_kwargs(
            name=GOVERNOR_PROFILE,
            workspace_dir=settings.governor_workspace_dir,
            claude_root=settings.governor_claude_root,
            observation="runtime.governor",
            max_turns=16,
            settings=settings,
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
    readable_paths: tuple[Path, ...] | None = None,
    denied_paths: tuple[Path, ...] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "role": name,
        "workspace_dir": workspace_dir,
        "claude_root": claude_root,
        "claude_config_dir": claude_root / ".claude",
        "data_dir": settings.data_dir,
        "mcp_config_path": workspace_dir / ".mcp.json",
        "project_settings_path": workspace_dir / ".claude" / "settings.json",
        "langfuse_observation_name": observation,
        "readable_paths": readable_paths or (),
        "writable_paths": (),
        "denied_paths": denied_paths or (business_agents_root(settings.data_dir),),
        "max_turns": max_turns,
    }
