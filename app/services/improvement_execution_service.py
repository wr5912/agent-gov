"""改进事项执行记录的治理 Agent 自动 apply + 版本生成服务（四阶段改进治理 §17.5 引擎波次第二阶段）。

把已确认优化方案交给治理 Agent governor 生成执行操作（ExecutionPlanFormatterOutput.operations），
复用既有安全原语在**隔离的 change set worktree** 上落盘 → 提交 → 生成候选 Agent 版本，
并把 change_set_id / applied_agent_version_id / applied_diff 权威绑定到 ExecutionRecord。

安全策略：
- 隔离：所有写入发生在 change set 的独立 git worktree，不直接动 Agent 主工作区。
- 原子+回滚：apply_execution_operations 自带写前快照与失败回滚。
- 幂等：ExecutionRecord 已绑定 applied_agent_version_id 时直接返回，不重复 apply。
- 失败/governor 拒绝（status≠ready 或无 operations）→ 放弃 change set（abandon_change_set）+ 回退启发式（不 apply、generated_by=heuristic）。
- 发布仍走既有 §12 发布门禁；本服务只生成候选版本，不自动发布到 Agent 当前版本。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.runtime.agent_job_types import AgentJobType, FormatterOutputModel, agent_job_spec
from app.runtime.execution_content_guards import guard_execution_write
from app.runtime.execution_targets import WorkspaceExecutionTargetPolicy
from app.runtime.json_types import JsonObject
from app.runtime.stores.improvement_content_store import ExecutionRecord, ImprovementContentStore
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.agent_governance import AgentGovernanceService
from app.services.workspace_execution_applier import WorkspaceExecutionApplier

logger = logging.getLogger(__name__)

RunProfileJson = Callable[..., Awaitable[FormatterOutputModel]]
# 受治理 apply 的可写配置目标：CLAUDE.md（prompt/角色）+ .claude/settings.json（权限）+ .mcp.json（工具）+ 现存 skills。
# 全部仍经隔离 worktree + 结构化护栏（guard_execution_write）+ change set 审批；不给 governor 直写。
_BASE_CONFIG_TARGETS = ["CLAUDE.md", ".claude/settings.json", ".mcp.json"]
_MAX_SKILL_TARGETS = 12


def _editable_config_targets(worktree: Path) -> list[str]:
    targets = list(_BASE_CONFIG_TARGETS)
    skills_dir = worktree / ".claude" / "skills"
    if skills_dir.is_dir():
        for skill_md in sorted(skills_dir.glob("*/SKILL.md"))[:_MAX_SKILL_TARGETS]:
            targets.append(skill_md.relative_to(worktree).as_posix())
    return targets


class ImprovementExecutionService:
    """优化方案 → governor 执行 → 隔离 worktree apply → 候选 Agent 版本 → 绑定 ExecutionRecord。"""

    def __init__(
        self,
        *,
        improvement_store: ImprovementStore,
        content_store: ImprovementContentStore,
        agent_governance: AgentGovernanceService,
        execution_app: WorkspaceExecutionApplier,
        run_profile_json: RunProfileJson | None,
    ) -> None:
        self._improvements = improvement_store
        self._content = content_store
        self._gov = agent_governance
        self._execution_app = execution_app
        self._run_profile_json = run_profile_json

    async def generate_and_apply_execution(self, improvement_id: str) -> ExecutionRecord:
        existing = self._content.get_execution(improvement_id)
        if existing and existing.applied_agent_version_id:
            return existing  # 幂等：已应用并生成版本
        item = self._improvements.get_improvement(improvement_id)
        plan = self._content.get_optimization_plan(improvement_id)
        if self._run_profile_json is not None and plan is not None and getattr(plan, "status", "") == "confirmed":
            try:
                return await self._governor_apply(item, plan, improvement_id)
            except Exception:  # noqa: BLE001 — 任何失败都回退启发式，保证 /execute 可用且工作区已回滚
                logger.exception("improvement governor execution apply failed: %s", improvement_id)
        return self._heuristic(plan, improvement_id)

    async def _governor_apply(self, item: Any, plan: Any, improvement_id: str) -> ExecutionRecord:
        agent_id = getattr(item, "agent_id", "main-agent") or "main-agent"
        change_set = self._gov.create_change_set(
            agent_id=agent_id,
            title=f"Improvement execution {improvement_id}",
            note=f"改进事项 {improvement_id} 自动执行优化方案候选。",
        )
        change_set_id = str(change_set["change_set_id"])
        trace_ref: dict[str, str] = {}
        try:
            worktree = self._gov.change_set_worktree_path(change_set)
            store = self._gov._store_for(agent_id)
            pre_version = store.version_summary(str(change_set["base_commit_sha"]), reason="improvement_execution_base", note=None)
            policy = WorkspaceExecutionTargetPolicy(worktree)
            output = await self._run_execution_governor(plan, policy, trace_ref=trace_ref)
            data = output.model_dump() if hasattr(output, "model_dump") else dict(output)
            operations = data.get("operations") or []
            if data.get("status") != "ready" or not operations:
                self._gov.abandon_change_set(change_set_id, note="governor 未产出可应用执行操作")
                return self._heuristic(plan, improvement_id, reason=str(data.get("no_action_reason") or ""))
            self._execution_app.apply_execution_operations(operations, workspace_dir=worktree, target_policy=policy, content_guard=guard_execution_write)
            applied_version, applied_diff = self._commit_candidate(store, worktree, pre_version, change_set_id, improvement_id)
        except Exception:
            try:
                self._gov.abandon_change_set(change_set_id, note="执行应用失败，已放弃候选变更集并回滚 worktree")
            except Exception:  # noqa: BLE001
                logger.exception("failed to abandon change set after execution failure: %s", change_set_id)
            raise
        execution = self._content.upsert_execution(
            improvement_id,
            summary=str(data.get("summary") or "已应用优化方案并生成候选 Agent 版本（待 §12 发布门禁发布）。"),
            changes_applied=[self._op_label(op) for op in operations],
            agent_version=str(applied_version.get("agent_version_id") or ""),
            risk_level=str(data.get("risk") or ""),
            rollback_strategy="回滚到执行前基线 Agent 版本（一键覆盖候选）",
            rollback_instructions=["放弃候选变更集 change_set", "恢复到执行前的 Agent 版本", "重新观测验证关键指标不劣于基线"],
            generated_by="governor",
            change_set_id=change_set_id,
            applied_agent_version_id=str(applied_version.get("agent_version_id") or ""),
            applied_diff=applied_diff or {},
            generation_trace_id=trace_ref.get("trace_id", ""),
            generation_trace_url=trace_ref.get("trace_url", ""),
        )
        self._improvements.add_link(improvement_id, kind="change_set", ref_id=change_set_id)
        return execution

    async def _run_execution_governor(
        self,
        plan: Any,
        policy: WorkspaceExecutionTargetPolicy,
        *,
        trace_ref: dict[str, str] | None = None,
    ) -> FormatterOutputModel:
        spec = agent_job_spec(AgentJobType.EXECUTION)
        changes = [c for c in (getattr(plan, "changes", []) or []) if isinstance(c, dict)]
        recommendations = [str(c.get("change", "")).strip() for c in changes if c.get("change")]
        plan_summary = getattr(plan, "summary", "") or "优化方案"
        target_type = str((changes[0].get("target") if changes else "prompt") or "prompt")
        targets = _editable_config_targets(policy.workspace_dir)
        primary = targets[0]
        job_input: JsonObject = {
            "proposal": {
                "title": plan_summary[:200],
                "description": plan_summary,
                "objective": plan_summary,
                "recommendation": "；".join(recommendations) or plan_summary,
                "recommended_actions": recommendations or [plan_summary],
                "target_type": target_type,
                "target_path": primary,
                "target_summary": f"在 {primary} 等可写配置资产落实：{plan_summary}",
            },
            "target_paths": targets,
            "target_policy": policy.policy_json(),
            "target_file_contexts": policy.file_contexts(targets),
        }
        prompt = spec.prompt_builder(job_input)
        assert self._run_profile_json is not None
        return await self._run_profile_json(
            profile_name=spec.profile_name,
            prompt=prompt,
            job_type=str(spec.job_type),
            job_input=job_input,
            governor={
                "job_type": str(spec.job_type),
                "scope_kind": "improvement",
                "scope_id": getattr(plan, "improvement_id", ""),
                "job_id": f"{spec.job_type}:{getattr(plan, 'improvement_id', '')}",
            },
            trace_callback=trace_ref.update if trace_ref is not None else None,
        )

    def _commit_candidate(
        self, store: Any, worktree: Any, pre_version: JsonObject, change_set_id: str, improvement_id: str
    ) -> tuple[JsonObject, JsonObject | None]:
        candidate = store.commit_worktree(worktree, message=f"Improvement {improvement_id} execution apply")
        applied_version = store.version_summary(candidate, reason="improvement_execution_candidate", note=f"改进事项 {improvement_id} 执行候选提交。")
        applied_diff = store.diff_versions(str(pre_version.get("agent_version_id") or ""), str(applied_version.get("agent_version_id") or ""))
        self._gov.mark_candidate_committed(change_set_id, candidate_commit_sha=candidate, execution_job_id=None, note=None)
        return applied_version, applied_diff

    @staticmethod
    def _op_label(op: object) -> str:
        if isinstance(op, dict):
            return f"{op.get('operation', 'edit')}: {op.get('path', '')}".strip()
        return str(op)

    def _heuristic(self, plan: Any, improvement_id: str, reason: str = "") -> ExecutionRecord:
        changes = [f"{c.get('target', '')}：{c.get('change', '')}" for c in (getattr(plan, "changes", []) or []) if isinstance(c, dict)]
        detail = reason or "governor 不可用或未产出可应用变更，需人工执行优化方案"
        return self._content.upsert_execution(
            improvement_id,
            summary=f"未自动应用：{detail}。",
            changes_applied=changes,
            agent_version="",
            risk_level="",
            rollback_strategy="未应用变更，无需回滚",
            rollback_instructions=[],
            generated_by="heuristic",
        )
