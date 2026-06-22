"""改进事项归因/优化方案的治理 Agent 生成服务（v2.7 §17.5 引擎波次）。

复用既有 governor 引擎（`agent_job_spec` 的 prompt-builder + formatter + 注入的 `run_profile_json`），
按 improvement 作用域生成 Attribution / OptimizationPlan 内容子资源。

字段所有权：improvement / NormalizedFeedback / Feedback 为 backend-owned 输入（作为 prompt grounding，不要求 LLM 输出）；
formatter 输出（rationale / responsibility_boundary / tasks 等）为 agent-owned。generated_by 为 boundary-owned 标注。

离线/健壮性：governor 不可用或调用失败时回退到确定性启发式（与旧 `/generate` 同口径），保证 `/generate` 不依赖远程服务。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypedDict

from app.runtime.agent_job_types import AgentJobType, FormatterOutputModel, agent_job_spec
from app.runtime.json_types import JsonObject
from app.runtime.stores.improvement_content_store import (
    AttributionRecord,
    ImprovementContentStore,
    OptimizationPlanRecord,
    RegressionAssessmentRecord,
)
from app.runtime.stores.improvement_store import ImprovementStore

RunProfileJson = Callable[..., Awaitable[FormatterOutputModel]]


class OptimizationChangeItem(TypedDict):
    target: str
    change: str


class RegressionCaseItem(TypedDict):
    prompt: str
    expected_behavior: str
    checkpoints: list[str]


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


class ImprovementGovernorService:
    """以 governor LLM 生成改进事项归因/方案；失败回退确定性启发式。"""

    def __init__(
        self,
        *,
        improvement_store: ImprovementStore,
        content_store: ImprovementContentStore,
        run_profile_json: RunProfileJson | None,
    ) -> None:
        self._improvements = improvement_store
        self._content = content_store
        self._run_profile_json = run_profile_json

    # ---- 归因 ----
    async def generate_attribution(self, improvement_id: str) -> AttributionRecord:
        item = self._improvements.get_improvement(improvement_id)
        nf = self._content.get_normalized_feedback(improvement_id)
        feedbacks = self._content.list_feedbacks(improvement_id)
        summary, boundary, evidence, generated_by = self._heuristic_attribution(item, nf)
        if self._run_profile_json is not None:
            try:
                output = await self._run_governor(
                    AgentJobType.ATTRIBUTION,
                    self._build_attribution_input(item, nf, feedbacks),
                    improvement_id,
                )
                summary, boundary, evidence = self._map_attribution(output, summary, boundary, evidence)
                generated_by = "governor"
            except Exception:  # noqa: BLE001 — 任何 governor 失败都回退确定性，保证可用
                pass
        return self._content.upsert_attribution(
            improvement_id, summary=summary, responsibility_boundary=boundary, evidence=evidence, generated_by=generated_by,
        )

    # ---- 优化方案 ----
    async def generate_optimization_plan(self, improvement_id: str) -> OptimizationPlanRecord:
        item = self._improvements.get_improvement(improvement_id)
        nf = self._content.get_normalized_feedback(improvement_id)
        attr = self._content.get_attribution(improvement_id)
        summary, changes, generated_by = self._heuristic_plan(item, nf, attr)
        if self._run_profile_json is not None:
            try:
                output = await self._run_governor(
                    AgentJobType.BATCH_PLAN,
                    self._build_plan_input(item, nf, attr),
                    improvement_id,
                )
                summary, changes = self._map_plan(output, summary, changes)
                generated_by = "governor"
            except Exception:  # noqa: BLE001
                pass
        return self._content.upsert_optimization_plan(
            improvement_id, summary=summary, changes=changes, generated_by=generated_by,
        )

    # ---- 回归保障评估（§11/§17.5）----
    async def generate_regression_assessment(self, improvement_id: str) -> RegressionAssessmentRecord:
        item = self._improvements.get_improvement(improvement_id)
        nf = self._content.get_normalized_feedback(improvement_id)
        feedbacks = self._content.list_feedbacks(improvement_id)
        summary, cases, generated_by = self._heuristic_regression(item, nf)
        if self._run_profile_json is not None:
            try:
                output = await self._run_governor(
                    AgentJobType.EVAL_CASE_GENERATION,
                    self._build_regression_input(item, nf, feedbacks),
                    improvement_id,
                )
                summary, cases = self._map_regression(output, summary, cases)
                generated_by = "governor"
            except Exception:  # noqa: BLE001
                pass
        return self._content.upsert_regression_assessment(improvement_id, summary=summary, cases=cases, generated_by=generated_by)

    @staticmethod
    def _build_regression_input(item: Any, nf: Any, feedbacks: list[Any]) -> JsonObject:
        problem = getattr(nf, "problem", "") if nf else getattr(item, "title", "")
        return {
            "feedback_cases": [
                {
                    "title": getattr(item, "title", ""),
                    "problem": problem,
                    "possible_object": getattr(nf, "possible_object", "") if nf else "",
                    "user_quote": getattr(nf, "user_quote", "") if nf else "",
                    "feedbacks": [{"summary": getattr(f, "summary", ""), "raw_text": getattr(f, "raw_text", "")} for f in feedbacks],
                }
            ],
            "source_refs": [{"kind": "improvement", "id": getattr(item, "improvement_id", "")}],
            "existing_eval_cases": [],
        }

    @staticmethod
    def _map_regression(output: FormatterOutputModel, summary: str, cases: list[RegressionCaseItem]) -> tuple[str, list[RegressionCaseItem]]:
        d = output.model_dump() if hasattr(output, "model_dump") else dict(output)
        eval_cases = d.get("eval_cases") or []
        mapped: list[RegressionCaseItem] = []
        for c in eval_cases:
            if not isinstance(c, dict):
                continue
            prompt = _text(c.get("prompt"))
            if not prompt:
                continue
            checks = c.get("checks_json") or {}
            checkpoints = list(checks.values()) if isinstance(checks, dict) else []
            mapped.append(RegressionCaseItem(
                prompt=prompt,
                expected_behavior=_text(c.get("expected_behavior")),
                checkpoints=[str(x) for x in checkpoints][:6],
            ))
        new_summary = (_text(d.get("no_action_reason")) or summary) if not mapped else f"治理 Agent 生成 {len(mapped)} 条回归用例候选。"
        return new_summary, (mapped or cases)

    @staticmethod
    def _heuristic_regression(item: Any, nf: Any) -> tuple[str, list[RegressionCaseItem], str]:
        title = getattr(item, "title", "") if item else ""
        problem = getattr(nf, "problem", "") if nf else title
        case = RegressionCaseItem(
            prompt=f"复现场景：当出现「{title}」类情况时，请处理。",
            expected_behavior=f"正确识别并避免重演：{problem}。",
            checkpoints=["是否识别问题条件", "是否提示需核验数据源", "是否避免直接升级处置"],
        )
        return "回归保障候选（启发式）：1 条复现用例。", [case], "heuristic"

    # ---- governor 调用 ----
    async def _run_governor(self, job_type: AgentJobType, job_input: JsonObject, improvement_id: str) -> FormatterOutputModel:
        spec = agent_job_spec(job_type)
        assert self._run_profile_json is not None
        return await self._run_profile_json(
            profile_name=spec.profile_name,
            prompt=spec.prompt_builder(job_input),
            job_type=str(spec.job_type),
            job_input=job_input,
            governor={"job_type": str(spec.job_type), "scope_kind": "improvement", "scope_id": improvement_id},
        )

    # ---- prompt 输入（backend-owned grounding）----
    @staticmethod
    def _build_attribution_input(item: Any, nf: Any, feedbacks: list[Any]) -> JsonObject:
        return {
            "feedback_case": {
                "improvement_id": getattr(item, "improvement_id", ""),
                "title": getattr(item, "title", ""),
                "agent_id": getattr(item, "agent_id", ""),
                "problem": getattr(nf, "problem", "") if nf else "",
                "possible_reason": getattr(nf, "possible_reason", "") if nf else "",
                "possible_object": getattr(nf, "possible_object", "") if nf else "",
                "user_quote": getattr(nf, "user_quote", "") if nf else "",
                "feedbacks": [
                    {"summary": getattr(f, "summary", ""), "source": getattr(f, "source", ""), "raw_text": getattr(f, "raw_text", "")}
                    for f in feedbacks
                ],
            },
            "task": getattr(item, "title", ""),
            "main_agent_version_id": getattr(item, "agent_id", ""),
        }

    @staticmethod
    def _build_plan_input(item: Any, nf: Any, attr: Any) -> JsonObject:
        return {
            "batch": {
                "improvement_id": getattr(item, "improvement_id", ""),
                "title": getattr(item, "title", ""),
                "attribution_summary": getattr(attr, "summary", "") if attr else "",
                "problem": getattr(nf, "problem", "") if nf else "",
                "possible_object": getattr(nf, "possible_object", "") if nf else "",
                "suggestion": getattr(nf, "suggestion", "") if nf else "",
            },
            "task": getattr(item, "title", ""),
            "main_agent_version_id": getattr(item, "agent_id", ""),
        }

    # ---- formatter 输出映射（agent-owned）----
    @staticmethod
    def _map_attribution(output: FormatterOutputModel, summary: str, boundary: list[str], evidence: list[str]) -> tuple[str, list[str], list[str]]:
        d = output.model_dump() if hasattr(output, "model_dump") else dict(output)
        rationale = _text(d.get("rationale")) or summary
        confidence = _text(d.get("confidence"))
        new_summary = f"{rationale}（置信度 {confidence}）" if confidence else rationale
        rb = d.get("responsibility_boundary") or {}
        new_boundary = boundary
        if isinstance(rb, dict) and (rb.get("owner") or rb.get("reason")):
            new_boundary = [f"{_text(rb.get('owner')) or '责任方'}：{_text(rb.get('reason'))}"]
        refs = d.get("evidence_refs") or []
        new_evidence = [
            f"{_text(r.get('type'))}:{_text(r.get('id'))} — {_text(r.get('reason'))}".strip(" :—")
            for r in refs if isinstance(r, dict)
        ] or evidence
        return new_summary, new_boundary, new_evidence

    @staticmethod
    def _map_plan(output: FormatterOutputModel, summary: str, changes: list[OptimizationChangeItem]) -> tuple[str, list[OptimizationChangeItem]]:
        d = output.model_dump() if hasattr(output, "model_dump") else dict(output)
        new_summary = _text(d.get("summary")) or _text(d.get("recommendation")) or summary
        tasks = d.get("tasks") or []
        mapped: list[OptimizationChangeItem] = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            target = _text(t.get("target_type")) or _text(t.get("target_path")) or "prompt"
            change = _text(t.get("recommendation")) or _text(t.get("summary")) or _text(t.get("title")) or _text(t.get("description"))
            if change:
                mapped.append(OptimizationChangeItem(target=target, change=change))
        return new_summary, (mapped or changes)

    # ---- 确定性回退（与旧 /generate 同口径）----
    @staticmethod
    def _heuristic_attribution(item: Any, nf: Any) -> tuple[str, list[str], list[str], str]:
        title = getattr(item, "title", "") if item else ""
        if nf:
            obj = getattr(nf, "possible_object", "") or "外部数据/工具"
            reason = getattr(nf, "possible_reason", "")
            summary = f"可能与「{obj}」相关：{getattr(nf, 'problem', '')}" + (f"（{reason}）" if reason else "") + "。"
            boundary = ["不是主 Agent 推理错误", f"主要可能在：{getattr(nf, 'possible_object', '') or '外部数据源 / 工具质量'}"]
            quote = getattr(nf, "user_quote", "")
            evidence = [f"用户反馈：{quote}"] if quote else []
        else:
            summary = f"针对「{title}」的初步归因，待补充系统理解和证据。"
            boundary = ["归因对象待确认"]
            evidence = []
        return summary, boundary, evidence, "heuristic"

    @staticmethod
    def _heuristic_plan(item: Any, nf: Any, attr: Any) -> tuple[str, list[OptimizationChangeItem], str]:
        title = getattr(item, "title", "") if item else ""
        base = getattr(attr, "summary", "") if attr else (getattr(nf, "suggestion", "") if nf else "")
        summary = f"针对「{title}」：{base or '补充校验/提示，避免重演该问题'}。"
        changes: list[OptimizationChangeItem] = [OptimizationChangeItem(target="prompt", change="补充对应校验与提示指令，避免重演该问题")]
        return summary, changes, "heuristic"
