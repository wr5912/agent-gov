"""改进执行 saga：持久化 intent 后在隔离 worktree 生成并绑定候选版本。"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TypedDict

from app.runtime.agent_git_store import AgentGitError
from app.runtime.agent_job_types import AgentJobType, FormatterOutputModel, agent_job_spec
from app.runtime.errors import BusinessRuleViolation, ConflictError, DataIntegrityError, RuntimeUnavailableError
from app.runtime.execution_content_guards import guard_execution_write
from app.runtime.execution_targets import WorkspaceExecutionTargetPolicy
from app.runtime.json_types import JsonObject
from app.runtime.protected_business_agents import DEFAULT_BUSINESS_AGENT_ID
from app.runtime.stores.improvement_content_store import ExecutionRecord, ImprovementContentStore
from app.runtime.stores.improvement_execution_claim_store import ExecutionClaim
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.agent_change_set_provisioner import ChangeSetSource
from app.services.agent_governance import AgentGovernanceError, AgentGovernanceService
from app.services.generated_agent_tests import build_generated_agent_test
from app.services.workspace_execution_applier import WorkspaceExecutionApplier

logger = logging.getLogger(__name__)

RunProfileJson = Callable[..., Awaitable[FormatterOutputModel]]
_BASE_CONFIG_TARGETS = ["CLAUDE.md", ".claude/settings.json", ".mcp.json"]
_MAX_SKILL_TARGETS = 12
_INVALID_CHANGE_SET_STATES = {"rejected", "abandoned", "failed"}
_EXECUTION_CLAIM_TTL_SECONDS = 600


class _ExecutionPlanChange(TypedDict):
    target: str
    change: str


def _scoped_execution_recommendations(
    changes: list[_ExecutionPlanChange],
    targets: list[str],
) -> list[tuple[str, str]]:
    scoped: list[tuple[str, str]] = []
    for change in changes:
        recommendation = str(change.get("change") or "").strip()
        target = _editable_target_for_hint(str(change.get("target") or ""), targets)
        if recommendation and target:
            scoped.append((target, recommendation))
    return scoped


def _editable_target_for_hint(target_hint: str, targets: list[str]) -> str | None:
    hint = Path(target_hint.strip().replace("\\", "/")).as_posix().casefold()
    if not hint:
        return None
    for target in targets:
        normalized = Path(target).as_posix().casefold()
        aliases = {normalized}
        if normalized.startswith(".claude/"):
            aliases.add(normalized.removeprefix(".claude/"))
        if any(alias in hint for alias in aliases):
            return target
    generic = hint.strip()
    if generic in {"prompt", "system_prompt"}:
        return next((target for target in targets if Path(target).as_posix() == "CLAUDE.md"), None)
    if generic in {"mcp", "mcp_config"}:
        return next((target for target in targets if Path(target).as_posix() == ".mcp.json"), None)
    if generic in {"runtime_config", "settings"}:
        return next((target for target in targets if Path(target).as_posix() == ".claude/settings.json"), None)
    if generic == "skill":
        return next((target for target in targets if Path(target).as_posix().startswith(".claude/skills/")), None)
    return None


@dataclass(frozen=True)
class CandidateEvidence:
    commit_sha: str
    agent_version_id: str
    applied_diff: JsonObject
    changes_applied: list[str]


def _editable_config_targets(worktree: Path) -> list[str]:
    targets = list(_BASE_CONFIG_TARGETS)
    skills_dir = worktree / ".claude" / "skills"
    if skills_dir.is_dir():
        for skill_md in sorted(skills_dir.glob("*/SKILL.md"))[:_MAX_SKILL_TARGETS]:
            targets.append(skill_md.relative_to(worktree).as_posix())
    return targets


class ImprovementExecutionService:
    """优化方案 -> fenced claim -> change set -> candidate evidence -> ExecutionRecord。"""

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
        self._claims = content_store.execution_claims
        self._gov = agent_governance
        self._execution_app = execution_app
        self._run_profile_json = run_profile_json

    async def generate_and_apply_execution(self, improvement_id: str) -> ExecutionRecord:
        plan = self._content.get_optimization_plan(improvement_id)
        if plan is None:
            raise BusinessRuleViolation(f"No optimization plan for improvement: {improvement_id}")
        if plan.status != "confirmed":
            raise ConflictError(f"Execution requires a confirmed optimization plan: {improvement_id}")
        attribution = self._content.get_attribution(improvement_id)
        if attribution is not None and attribution.status != "confirmed":
            raise ConflictError(f"Execution requires confirmed attribution when attribution exists: {improvement_id}")
        item = self._improvements.get_improvement(improvement_id)
        if item is None:
            raise BusinessRuleViolation(f"No improvement item: {improvement_id}")
        existing = self._content.get_execution(improvement_id)
        if _has_applied_execution(existing):
            if not _execution_matches_source(existing, plan=plan, attribution=attribution):
                raise ConflictError(f"Applied execution belongs to a different plan or attribution revision: {improvement_id}")
            self._ensure_change_set_link(existing)
            return existing  # type: ignore[return-value]
        agent_id = getattr(item, "agent_id", DEFAULT_BUSINESS_AGENT_ID) or DEFAULT_BUSINESS_AGENT_ID
        store = self._gov._store_for(agent_id)
        change_set_id, base_commit_sha = self._execution_intent(existing, store)
        now, lease_expires_at = _lease_window()
        claim = self._claims.claim_execution(
            improvement_id,
            change_set_id=change_set_id,
            base_commit_sha=base_commit_sha,
            source_optimization_plan_id=plan.optimization_plan_id,
            source_optimization_plan_updated_at=plan.updated_at,
            source_attribution_id=attribution.attribution_id if attribution else "",
            source_attribution_updated_at=attribution.updated_at if attribution else "",
            claim_token=uuid.uuid4().hex,
            now=now,
            claim_expires_at=lease_expires_at,
        )
        if self._run_profile_json is None:
            return self._finish_without_application(claim, "governor 不可用，未自动应用优化方案", retain_change_set=False)
        try:
            return await self._execute_claim(
                claim,
                plan=plan,
                attribution=attribution,
                agent_id=agent_id,
                store=store,
            )
        except Exception as exc:
            return self._handle_execution_failure(claim, store=store, error=exc)

    def reconcile_expired_executions(self, *, limit: int = 100) -> JsonObject:
        now = datetime.now(timezone.utc).isoformat()
        recovered = released = skipped = failed = 0
        for expired in self._claims.list_expired_claims(now=now, limit=limit):
            try:
                item = self._improvements.get_improvement(expired.improvement_id)
                if item is None:
                    raise DataIntegrityError(f"Expired execution has no improvement item: {expired.improvement_id}")
                _, claim_expires_at = _lease_window()
                claim = self._claims.claim_execution(
                    expired.improvement_id,
                    change_set_id=expired.change_set_id,
                    base_commit_sha=expired.base_commit_sha,
                    source_optimization_plan_id=expired.source_optimization_plan_id,
                    source_optimization_plan_updated_at=expired.source_optimization_plan_updated_at,
                    source_attribution_id=expired.source_attribution_id,
                    source_attribution_updated_at=expired.source_attribution_updated_at,
                    claim_token=uuid.uuid4().hex,
                    now=now,
                    claim_expires_at=claim_expires_at,
                )
                store = self._gov._store_for(getattr(item, "agent_id", DEFAULT_BUSINESS_AGENT_ID) or DEFAULT_BUSINESS_AGENT_ID)
                if self._candidate_commit_exists(claim, store=store):
                    try:
                        self._recover_candidate_claim(claim, store=store)
                    except Exception as exc:  # noqa: BLE001 - always release a corrupt durable fence.
                        self._finish_without_application(
                            claim,
                            f"待发布版本自动对账失败：{exc.__class__.__name__}: {exc}",
                            retain_change_set=True,
                        )
                        failed += 1
                    else:
                        recovered += 1
                    continue
                retain_change_set = self._compensate_unapplied_change_set(
                    claim,
                    store=store,
                    reason="过期执行申请未发现待发布版本，后台对账已释放。",
                )
                self._finish_without_application(
                    claim,
                    "过期执行申请未发现待发布版本，已释放执行锁。",
                    retain_change_set=retain_change_set,
                )
                released += 1
            except ConflictError:
                skipped += 1
            except Exception:  # noqa: BLE001 - isolate corrupt rows so later claims still reconcile.
                failed += 1
                logger.exception("expired improvement execution reconciliation failed: %s", expired.improvement_id)
        return {"recovered": recovered, "released": released, "skipped": skipped, "failed": failed}

    async def _execute_claim(
        self,
        claim: ExecutionClaim,
        *,
        plan: Any,
        attribution: Any,
        agent_id: str,
        store: Any,
    ) -> ExecutionRecord:
        change_set = self._gov.create_change_set(
            agent_id=agent_id,
            execution_job_id=claim.execution_id,
            base_commit_sha=claim.base_commit_sha,
            change_set_id=claim.change_set_id,
            title=f"Improvement execution {claim.improvement_id}",
            note=f"改进事项 {claim.improvement_id} 自动执行优化方案候选。",
            source=ChangeSetSource(
                improvement_id=claim.improvement_id,
                attribution_id=attribution.attribution_id if attribution else None,
                attribution_status=attribution.status if attribution else None,
            ),
        )
        self._validate_change_set_intent(claim, change_set)
        evidence = self._candidate_evidence(claim, change_set=change_set, store=store)
        if evidence is not None:
            return self._finalize_candidate(claim, evidence=evidence, summary="已对账恢复中断的候选 Agent 版本。")
        worktree = self._gov.change_set_worktree_path(change_set)
        store.reset_worktree(worktree, base_ref=claim.base_commit_sha)
        policy = WorkspaceExecutionTargetPolicy(worktree)
        editable_targets = _editable_config_targets(worktree)
        plan_changes = [
            _ExecutionPlanChange(target=str(change.get("target") or ""), change=str(change.get("change") or ""))
            for change in (getattr(plan, "changes", []) or [])
            if isinstance(change, dict)
        ]
        scoped_recommendations = _scoped_execution_recommendations(plan_changes, editable_targets)
        targets = list(dict.fromkeys(target for target, _ in scoped_recommendations))
        if not targets:
            return self._abandon_no_action(claim, store=store, reason="已确认优化方案没有可确定映射的 Workspace 配置目标")
        trace_ref: dict[str, str] = {}
        output = await self._run_execution_governor(plan, policy, targets, trace_ref=trace_ref)
        data = output.model_dump() if hasattr(output, "model_dump") else dict(output)
        operations = data.get("operations") or []
        if data.get("status") != "ready" or not operations:
            reason = str(data.get("no_action_reason") or "governor 未产出可应用执行操作")
            return self._abandon_no_action(claim, store=store, reason=reason)
        self._renew_claim(claim)
        with store.mutation_guard():
            self._execution_app.apply_execution_operations(
                operations,
                workspace_dir=worktree,
                target_policy=policy,
                content_guard=guard_execution_write,
                workspace_guard=lambda candidate: self._gov.require_workspace_policy(candidate, agent_id),
                allowed_targets=set(targets),
            )
            self._renew_claim(claim)
            candidate = store.commit_worktree(worktree, message=f"Improvement {claim.improvement_id} execution apply")
        self._gov.mark_candidate_committed(
            claim.change_set_id,
            candidate_commit_sha=candidate,
            execution_job_id=claim.execution_id,
            note=None,
        )
        refreshed = self._gov.get_change_set(claim.change_set_id) or change_set
        evidence = self._candidate_evidence(claim, change_set=refreshed, store=store)
        if evidence is None:
            raise DataIntegrityError("Candidate commit was not recoverable after execution apply")
        return self._finalize_candidate(
            claim,
            evidence=evidence,
            summary=str(data.get("summary") or "已应用优化方案并生成候选 Agent 版本。"),
            risk_level=str(data.get("risk") or ""),
            changes_applied=[self._op_label(op) for op in operations],
            trace_ref=trace_ref,
        )

    def _execution_intent(self, existing: ExecutionRecord | None, store: Any) -> tuple[str, str]:
        if existing and existing.change_set_id:
            change_set = self._gov.get_change_set(existing.change_set_id)
            if change_set and str(change_set.get("status")) not in _INVALID_CHANGE_SET_STATES:
                base = str(change_set.get("base_commit_sha") or existing.base_commit_sha)
                if not base:
                    raise DataIntegrityError("Existing execution change set has no base commit")
                return existing.change_set_id, base
            if change_set:
                self._cleanup_change_set_worktree(change_set, store=store)
            elif existing.base_commit_sha:
                return existing.change_set_id, existing.base_commit_sha
        base = str(store.current_commit_sha() or "")
        if not base:
            raise ConflictError("Agent Git repository has no base commit")
        return f"agc-{uuid.uuid4()}", base

    def _candidate_evidence(self, claim: ExecutionClaim, *, change_set: JsonObject, store: Any) -> CandidateEvidence | None:
        self._validate_change_set_intent(claim, change_set)
        worktree = self._gov.change_set_worktree_path(change_set)
        candidate = str(change_set.get("candidate_commit_sha") or "")
        worktree_head = store.worktree_commit_sha(worktree)
        if not candidate and worktree_head and worktree_head != claim.base_commit_sha:
            change_set = self._gov.mark_candidate_committed(
                claim.change_set_id,
                candidate_commit_sha=worktree_head,
                execution_job_id=claim.execution_id,
                note="对账恢复中断的改进执行候选提交。",
                operator="improvement-reconciler",
            )
            candidate = str(change_set.get("candidate_commit_sha") or "")
        if not candidate or candidate == claim.base_commit_sha:
            return None
        applied_diff = store.diff_versions(claim.base_commit_sha, candidate)
        changes_applied = _diff_change_labels(applied_diff)
        if not applied_diff or not changes_applied:
            raise DataIntegrityError("Candidate commit has no verifiable diff evidence")
        version = store.version_summary(candidate, reason="improvement_execution_candidate", note=f"改进事项 {claim.improvement_id} 执行候选提交。")
        version_id = str(version.get("agent_version_id") or "")
        if not version_id:
            raise DataIntegrityError("Candidate commit has no Agent version id")
        return CandidateEvidence(candidate, version_id, applied_diff, changes_applied)

    def _finalize_candidate(
        self,
        claim: ExecutionClaim,
        *,
        evidence: CandidateEvidence,
        summary: str,
        risk_level: str = "",
        changes_applied: list[str] | None = None,
        trace_ref: dict[str, str] | None = None,
    ) -> ExecutionRecord:
        self._claims.finalize_execution_claim(
            claim.improvement_id,
            claim_token=claim.claim_token,
            claim_generation=claim.claim_generation,
            summary=summary,
            changes_applied=changes_applied or evidence.changes_applied,
            agent_version=evidence.agent_version_id,
            risk_level=risk_level,
            rollback_strategy="回滚到执行前基线 Agent 版本",
            rollback_instructions=["放弃候选变更集", "恢复执行前 Agent 版本", "重新验证关键指标"],
            applied_diff=evidence.applied_diff,
            generation_trace_id=(trace_ref or {}).get("trace_id", ""),
            generation_trace_url=(trace_ref or {}).get("trace_url", ""),
        )
        record = self._require_execution(claim.improvement_id)
        self._ensure_change_set_link(record)
        return record

    def _handle_execution_failure(self, claim: ExecutionClaim, *, store: Any, error: Exception) -> ExecutionRecord:
        if self._candidate_commit_exists(claim, store=store):
            try:
                return self._recover_candidate_claim(claim, store=store)
            except Exception as recovery_error:
                try:
                    self._finish_without_application(
                        claim,
                        f"待发布版本对账失败：{recovery_error.__class__.__name__}: {recovery_error}",
                        retain_change_set=True,
                    )
                except ConflictError:
                    self._expire_claim(claim)
                raise error from recovery_error
        try:
            self._renew_claim(claim)
        except ConflictError as claim_conflict:
            raise error from claim_conflict
        detail = f"{error.__class__.__name__}: {error}"
        retain_change_set = self._compensate_unapplied_change_set(
            claim,
            store=store,
            reason=f"未自动应用：{detail}",
        )
        record = self._finish_after_compensation(claim, f"未自动应用：{detail}", retain_change_set=retain_change_set)
        if isinstance(error, (RuntimeUnavailableError, ConflictError, DataIntegrityError, AgentGitError, AgentGovernanceError)):
            raise error
        return record

    def materialize_regression_tests(self, improvement_id: str) -> JsonObject:
        assessment = self._content.get_regression_test_design(improvement_id)
        execution = self._content.get_execution(improvement_id)
        item = self._improvements.get_improvement(improvement_id)
        if assessment is None or not assessment.tests:
            raise BusinessRuleViolation(f"Regression test design is required: {improvement_id}")
        if execution is None or execution.status != "confirmed" or not _has_applied_execution(execution):
            raise ConflictError(f"Confirmed execution is required before generating tests: {improvement_id}")
        if item is None:
            raise BusinessRuleViolation(f"No improvement item: {improvement_id}")
        change_set = self._gov.get_change_set(execution.change_set_id)
        if change_set is None:
            raise DataIntegrityError(f"Execution change set disappeared: {execution.change_set_id}")
        previous_commit = str(change_set.get("candidate_commit_sha") or "")
        if not previous_commit or previous_commit != execution.applied_agent_version_id:
            raise ConflictError("待发布变更与已确认执行记录的 commit 不一致。")
        agent_id = getattr(item, "agent_id", DEFAULT_BUSINESS_AGENT_ID) or DEFAULT_BUSINESS_AGENT_ID
        store = self._gov._store_for(agent_id)
        worktree = self._gov.change_set_worktree_path(change_set)
        generated_files: list[tuple[str, str]] = []
        for index, test in enumerate(assessment.tests, start=1):
            candidate = build_generated_agent_test(
                improvement_id=improvement_id,
                index=index,
                test_code=str(test.get("test_code") or ""),
                test_intent=str(test.get("test_intent") or ""),
                assertion_rationale=str(test.get("assertion_rationale") or ""),
            )
            if candidate.target_path != str(test.get("target_path") or ""):
                raise DataIntegrityError("Stored regression test path no longer matches backend projection")
            generated_files.append((candidate.target_path, candidate.test_code))
        created = _create_generated_test_assets(worktree, files=generated_files)
        try:
            with store.mutation_guard():
                candidate = store.commit_squashed_worktree(
                    worktree,
                    base_ref=execution.base_commit_sha,
                    message=f"Improvement {improvement_id} add feedback regression pytest",
                )
            self._gov.mark_candidate_committed(
                execution.change_set_id,
                candidate_commit_sha=candidate,
                execution_job_id=execution.execution_id,
                note="确认配置与 pytest 测试文件为同一待发布版本。",
                operator="feedback-test-generator",
            )
            applied_diff = store.diff_versions(execution.base_commit_sha, candidate)
            if not applied_diff:
                raise DataIntegrityError("Generated tests have no verifiable candidate diff")
            self._content.rebind_execution_candidate(
                improvement_id,
                change_set_id=execution.change_set_id,
                previous_commit_sha=previous_commit,
                candidate_commit_sha=candidate,
                applied_diff=applied_diff,
                generated_test_files=created,
            )
        except Exception:
            try:
                with store.mutation_guard():
                    store.reset_worktree(worktree, base_ref=previous_commit)
                self._gov.mark_candidate_committed(
                    execution.change_set_id,
                    candidate_commit_sha=previous_commit,
                    execution_job_id=execution.execution_id,
                    note="pytest 测试资产生成失败，已恢复原待发布版本。",
                    operator="feedback-test-generator",
                )
            except Exception:  # noqa: BLE001 - preserve the original failure and log failed compensation.
                logger.exception("failed to restore change set after pytest asset generation error")
            raise
        return {
            "agent_id": agent_id,
            "change_set_id": execution.change_set_id,
            "candidate_commit_sha": candidate,
            "generated_test_files": created,
        }

    def _abandon_no_action(self, claim: ExecutionClaim, *, store: Any, reason: str) -> ExecutionRecord:
        retain_change_set = self._compensate_unapplied_change_set(
            claim,
            store=store,
            reason=f"未自动应用：{reason}",
        )
        return self._finish_after_compensation(claim, f"未自动应用：{reason}", retain_change_set=retain_change_set)

    def _compensate_unapplied_change_set(self, claim: ExecutionClaim, *, store: Any, reason: str) -> bool:
        change_set = self._gov.get_change_set(claim.change_set_id)
        if change_set is None:
            store.remove_worktree(claim.change_set_id)
            return False
        status = str(change_set.get("status") or "")
        if status not in _INVALID_CHANGE_SET_STATES:
            change_set = self._gov.abandon_change_set(
                claim.change_set_id,
                note=reason,
            )
        self._cleanup_change_set_worktree(change_set, store=store)
        return True

    def _cleanup_change_set_worktree(self, change_set: JsonObject, *, store: Any) -> None:
        if str(change_set.get("status") or "") not in _INVALID_CHANGE_SET_STATES:
            return
        try:
            store.remove_worktree(
                str(change_set["change_set_id"]),
                delete_branch=not bool(change_set.get("candidate_commit_sha")),
            )
        except Exception:  # noqa: BLE001 - DB terminal state is authoritative; a later retry repeats cleanup.
            logger.exception("failed to clean abandoned improvement worktree: %s", change_set.get("change_set_id"))

    def _finish_without_application(self, claim: ExecutionClaim, summary: str, *, retain_change_set: bool) -> ExecutionRecord:
        self._claims.finish_without_application(
            claim.improvement_id,
            claim_token=claim.claim_token,
            claim_generation=claim.claim_generation,
            summary=summary,
            retain_change_set=retain_change_set,
        )
        return self._require_execution(claim.improvement_id)

    def _finish_after_compensation(self, claim: ExecutionClaim, summary: str, *, retain_change_set: bool) -> ExecutionRecord:
        record = self._content.get_execution(claim.improvement_id)
        if record is not None and record.status != "applying":
            return record
        return self._finish_without_application(claim, summary, retain_change_set=retain_change_set)

    def _renew_claim(self, claim: ExecutionClaim) -> None:
        now, expires_at = _lease_window()
        self._claims.renew_execution_claim(
            claim.improvement_id,
            claim_token=claim.claim_token,
            claim_generation=claim.claim_generation,
            now=now,
            claim_expires_at=expires_at,
        )

    def _expire_claim(self, claim: ExecutionClaim) -> None:
        try:
            self._claims.expire_claim(
                claim.improvement_id,
                claim_token=claim.claim_token,
                claim_generation=claim.claim_generation,
                now=datetime.now(timezone.utc).isoformat(),
            )
        except ConflictError:
            return

    def _candidate_commit_exists(self, claim: ExecutionClaim, *, store: Any) -> bool:
        change_set = self._gov.get_change_set(claim.change_set_id)
        if change_set and change_set.get("candidate_commit_sha"):
            return True
        worktree = self._gov.change_set_worktree_path(change_set or {"worktree_path": str(store.worktrees_dir / claim.change_set_id)})
        try:
            head = store.worktree_commit_sha(worktree)
        except Exception:  # noqa: BLE001 - absence/corruption is handled as unapplied compensation.
            return False
        return bool(head and head != claim.base_commit_sha)

    def _recover_candidate_claim(self, claim: ExecutionClaim, *, store: Any) -> ExecutionRecord:
        existing = self._content.get_execution(claim.improvement_id)
        if _has_applied_execution(existing):
            self._ensure_change_set_link(existing)
            return existing  # type: ignore[return-value]
        change_set = self._gov.get_change_set(claim.change_set_id)
        if change_set is None:
            raise DataIntegrityError("Execution candidate change set disappeared during reconciliation")
        evidence = self._candidate_evidence(claim, change_set=change_set, store=store)
        if evidence is None:
            raise DataIntegrityError("Execution candidate evidence disappeared during reconciliation")
        return self._finalize_candidate(
            claim,
            evidence=evidence,
            summary="已自动对账恢复中断的候选 Agent 版本。",
        )

    def _ensure_change_set_link(self, record: ExecutionRecord | None) -> None:
        if record is None or not record.change_set_id:
            return
        for link in self._improvements.list_links(record.improvement_id):
            if link.kind == "change_set" and link.ref_id == record.change_set_id:
                return
        self._improvements.add_link(record.improvement_id, kind="change_set", ref_id=record.change_set_id)

    def _require_execution(self, improvement_id: str) -> ExecutionRecord:
        record = self._content.get_execution(improvement_id)
        if record is None:
            raise DataIntegrityError(f"Execution record disappeared: {improvement_id}")
        return record

    @staticmethod
    def _validate_change_set_intent(claim: ExecutionClaim, change_set: JsonObject) -> None:
        actual = (str(change_set.get("change_set_id") or ""), str(change_set.get("base_commit_sha") or ""))
        expected = (claim.change_set_id, claim.base_commit_sha)
        if actual != expected:
            raise DataIntegrityError("Execution intent no longer matches its Agent change set")

    async def _run_execution_governor(
        self,
        plan: Any,
        policy: WorkspaceExecutionTargetPolicy,
        targets: list[str],
        *,
        trace_ref: dict[str, str],
    ) -> FormatterOutputModel:
        spec = agent_job_spec(AgentJobType.EXECUTION)
        changes = [
            _ExecutionPlanChange(
                target=str(change.get("target") or ""),
                change=str(change.get("change") or ""),
            )
            for change in (getattr(plan, "changes", []) or [])
            if isinstance(change, dict)
        ]
        scoped_recommendations = _scoped_execution_recommendations(changes, targets)
        plan_summary = getattr(plan, "summary", "") or "优化方案"
        primary = scoped_recommendations[0][0] if scoped_recommendations else targets[0]
        recommendations = [recommendation for _, recommendation in scoped_recommendations]
        job_input: JsonObject = {
            "proposal": {
                "title": plan_summary[:200],
                "description": plan_summary,
                "objective": plan_summary,
                "recommendation": "；".join(recommendations) or plan_summary,
                "recommended_actions": recommendations or [plan_summary],
                "target_type": primary,
                "target_path": primary,
                "target_summary": f"在 {primary} 等可写配置资产落实：{plan_summary}",
            },
            "target_paths": targets,
            "target_policy": policy.policy_json(),
            "target_file_contexts": policy.file_contexts(targets),
        }
        assert self._run_profile_json is not None
        return await self._run_profile_json(
            profile_name=spec.profile_name,
            prompt=spec.prompt_builder(job_input),
            job_type=str(spec.job_type),
            job_input=job_input,
            governor={
                "job_type": str(spec.job_type),
                "scope_kind": "improvement",
                "scope_id": getattr(plan, "improvement_id", ""),
                "job_id": f"{spec.job_type}:{getattr(plan, 'improvement_id', '')}",
            },
            trace_callback=trace_ref.update,
        )

    @staticmethod
    def _op_label(operation: object) -> str:
        if isinstance(operation, dict):
            return f"{operation.get('operation', 'edit')}: {operation.get('path', '')}".strip()
        return str(operation)


def _create_generated_test_assets(worktree: Path, *, files: list[tuple[str, str]]) -> list[str]:
    workspace = worktree.resolve()
    tests_dir = workspace / "tests"
    if tests_dir.exists() and (not tests_dir.is_dir() or tests_dir.is_symlink()):
        raise ConflictError("Workspace tests path is not a regular directory")
    readme = tests_dir / "README.md"
    destinations: list[tuple[str, Path, str]] = []
    for relative_path, content in files:
        destination = workspace / relative_path
        if destination.parent != tests_dir:
            raise DataIntegrityError("Generated pytest path escaped the flat tests directory")
        if destination.exists() and (not destination.is_file() or destination.is_symlink() or destination.read_text(encoding="utf-8") != content):
            raise ConflictError(f"Generated pytest file already exists and cannot be overwritten: {relative_path}")
        destinations.append((relative_path, destination, content))
    if readme.exists() and (not readme.is_file() or readme.is_symlink()):
        raise ConflictError("Workspace tests/README.md is not a regular file")

    tests_dir_created = not tests_dir.exists()
    tests_dir.mkdir(parents=True, exist_ok=True)
    created: list[str] = []
    try:
        if not readme.exists():
            readme.write_text(
                "# Agent 测试套件\n\n本目录由业务 Agent 开发者维护，平台固定使用 pytest 执行。\n",
                encoding="utf-8",
            )
            created.append("tests/README.md")
        for relative_path, destination, content in destinations:
            if not destination.exists():
                destination.write_text(content, encoding="utf-8")
            created.append(relative_path)
        return created
    except Exception:
        for generated in reversed(created):
            (workspace / generated).unlink(missing_ok=True)
        if tests_dir_created:
            tests_dir.rmdir()
        raise


def _lease_window() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return now.isoformat(), (now + timedelta(seconds=_EXECUTION_CLAIM_TTL_SECONDS)).isoformat()


def _has_applied_execution(record: ExecutionRecord | None) -> bool:
    if record is None:
        return False
    bound_candidate = bool(record.change_set_id and record.applied_agent_version_id and record.applied_diff)
    manual_evidence = bool(record.changes_applied and record.agent_version.strip())
    return bound_candidate or manual_evidence


def _execution_matches_source(record: ExecutionRecord | None, *, plan: Any, attribution: Any) -> bool:
    if record is None:
        return False
    expected_attribution = (getattr(attribution, "attribution_id", ""), getattr(attribution, "updated_at", "")) if attribution is not None else ("", "")
    return (
        record.source_optimization_plan_id,
        record.source_optimization_plan_updated_at,
        record.source_attribution_id,
        record.source_attribution_updated_at,
    ) == (
        getattr(plan, "optimization_plan_id", ""),
        getattr(plan, "updated_at", ""),
        *expected_attribution,
    )


def _diff_change_labels(diff: object) -> list[str]:
    if not isinstance(diff, dict):
        return []
    labels: list[str] = []
    for action, key in (("add", "added"), ("edit", "modified"), ("delete", "deleted")):
        for item in diff.get(key) or []:
            path = item.get("path") if isinstance(item, dict) else item
            if path:
                labels.append(f"{action}: {path}")
    for path in diff.get("changed_files") or []:
        label = f"edit: {path}"
        if label not in labels:
            labels.append(label)
    return labels
