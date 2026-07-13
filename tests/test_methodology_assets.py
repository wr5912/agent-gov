"""AGV-006 / AGV-012：方法论资产是一等的命名+结构化契约，由版本治理的治理 Agent 承载。

AgentGov 的「方法论资产」（如何分析/如何优化/如何评估）以治理方法的结构化输出契约形式
沉淀：`AGENT_JOB_SPECS` 是单一来源，把每个治理方法映射到命名 profile + 独立的 Pydantic
formatter 契约（非自然语言）。这些契约由治理 Agent（`GOVERNANCE_AGENT_ROLES`）承载，
而治理 Agent 与 main agent 一样受 Git 版本治理（commit / change set / release 修订记录），并被每个
反馈 case 的同类方法复用（单一来源，非每次重新发明），满足「命名/适用范围/版本修订/复用」。

注意：不依赖已废弃的 `X/vN` schema-version 命名字符串（治理硬门 legacy 项），方法论的
形式化以 typed Pydantic 契约 + 版本治理 profile 为准。
"""

from __future__ import annotations

from app.runtime.agent_job_types import AGENT_JOB_SPECS, AgentJobType
from app.runtime.agent_profiles import GOVERNANCE_AGENT_ROLES

# 四个核心治理方法（如何分析 / 如何优化 / 如何执行 / 如何评估）。
_GOVERNANCE_METHODS = (
    AgentJobType.ATTRIBUTION,
    AgentJobType.OPTIMIZATION_PLAN,
    AgentJobType.EXECUTION,
    AgentJobType.EVAL_CASE_GENERATION,
)


def test_methodology_registry_is_single_source_named_and_structured():
    """AGV-012：方法论非散落 NL——每个治理方法有命名 profile + 独立结构化契约，单一来源复用。"""
    for job_type in _GOVERNANCE_METHODS:
        assert job_type in AGENT_JOB_SPECS, f"方法论单一来源应覆盖 {job_type}"
        spec = AGENT_JOB_SPECS[job_type]
        assert spec.profile_name, f"{job_type} 方法应有命名承载 profile"
        assert spec.formatter_output_model is not None, f"{job_type} 方法应有结构化输出契约（非自然语言）"

    # 各方法的结构化契约互不相同：方法论是差异化的形式契约，而非一份通用 NL 模板。
    models = [AGENT_JOB_SPECS[jt].formatter_output_model for jt in _GOVERNANCE_METHODS]
    assert len(set(models)) == len(models), "每个治理方法应有独立结构化契约"


def test_methodology_assets_are_carried_by_version_governed_governance_agents():
    """AGV-012/006：方法论由治理 Agent 承载，治理 Agent 受版本治理（提供版本/修订记录）。"""
    for job_type in _GOVERNANCE_METHODS:
        profile_name = AGENT_JOB_SPECS[job_type].profile_name
        # 方法论的承载者必须是受治理的治理 Agent 角色，而非临时/业务 Agent——
        # 治理 Agent 与 main agent 同样受 Git 版本治理，构成方法论的修订记录。
        assert profile_name in GOVERNANCE_AGENT_ROLES, (
            f"治理方法 {job_type} 的承载 profile {profile_name!r} 应是受版本治理的治理 Agent 角色"
        )
