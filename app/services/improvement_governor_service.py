"""改进事项归因/优化方案的治理 Agent 生成服务（四阶段改进治理 §17.5 引擎波次）。

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
    NormalizedFeedbackRecord,
    OptimizationPlanRecord,
    RegressionAssessmentRecord,
)
from app.runtime.stores.improvement_store import ImprovementStore

RunProfileJson = Callable[..., Awaitable[FormatterOutputModel]]
# 反馈整理不需要 governor（无工具/无多轮）：直接一次 DSPy formatter 把原始反馈归纳成 title+problem。
FormatNormalizedFeedback = Callable[[str], Awaitable[FormatterOutputModel]]


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
        format_normalized_feedback: FormatNormalizedFeedback | None = None,
    ) -> None:
        self._improvements = improvement_store
        self._content = content_store
        self._run_profile_json = run_profile_json
        self._format_normalized_feedback = format_normalized_feedback

    # ---- 系统理解 NormalizedFeedback（只整理反馈：一次 DSPy formatter，无 governor）----
    async def generate_normalized_feedback(self, improvement_id: str) -> NormalizedFeedbackRecord:
        item = self._improvements.get_improvement(improvement_id)
        feedbacks = self._content.list_feedbacks(improvement_id)
        existing = self._content.get_normalized_feedback(improvement_id)
        raw = self._feedback_text(feedbacks)
        title, problem, generated_by = self._heuristic_normalized_feedback(item, feedbacks)
        if self._format_normalized_feedback is not None and raw:
            try:
                output = await self._format_normalized_feedback(raw)
                data = output.model_dump() if hasattr(output, "model_dump") else dict(output)
                problem = _text(data.get("problem")) or problem
                title = _text(data.get("title")) or title
                generated_by = "llm"
            except Exception:  # noqa: BLE001 — formatter 不可用/校验失败回退启发式，保证可用
                pass
        user_quote = (getattr(feedbacks[0], "raw_text", "") if feedbacks else "") or (getattr(existing, "user_quote", "") if existing else "")
        record = self._content.upsert_normalized_feedback(
            improvement_id,
            problem=problem,
            # 原因/对象/影响是归因阶段的分析，不在整理阶段产出：保留既有值（或留空占位待归因）。
            possible_reason=getattr(existing, "possible_reason", "") if existing else "",
            possible_object=getattr(existing, "possible_object", "") if existing else "",
            impact=getattr(existing, "impact", "") if existing else "",
            suggestion=getattr(existing, "suggestion", "") if existing else "",
            user_quote=user_quote,
            generated_by=generated_by,
        )
        self._backfill_title(item, feedbacks, title)
        return record

    @staticmethod
    def _feedback_text(feedbacks: list[Any]) -> str:
        parts = [str(getattr(f, "raw_text", "") or getattr(f, "summary", "")).strip() for f in feedbacks]
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def _heuristic_normalized_feedback(item: Any, feedbacks: list[Any]) -> tuple[str, str, str]:
        raw = getattr(feedbacks[0], "raw_text", "") if feedbacks else ""
        problem = (getattr(feedbacks[0], "summary", "") if feedbacks else "") or raw or getattr(item, "title", "")
        title = getattr(item, "title", "") or problem
        return title, problem, "heuristic"

    def _backfill_title(self, item: Any, feedbacks: list[Any], title: str) -> None:
        """把整理生成的简洁 title 回填到 improvement.title——仅当现 title 仍是自动截断态（空或原文前缀），不覆盖用户手改。"""
        new_title = _text(title)
        improvement_id = getattr(item, "improvement_id", "")
        if not new_title or not improvement_id:
            return
        current = _text(getattr(item, "title", ""))
        raw = getattr(feedbacks[0], "raw_text", "") if feedbacks else ""
        is_auto = (not current) or (bool(raw) and raw.startswith(current))
        if is_auto and new_title != current:
            self._improvements.update_title(improvement_id, title=new_title)

    # ---- 归因 ----
    async def generate_attribution(self, improvement_id: str) -> AttributionRecord:
        item = self._improvements.get_improvement(improvement_id)
        nf = self._content.get_normalized_feedback(improvement_id)
        feedbacks = self._content.list_feedbacks(improvement_id)
        summary, boundary, evidence, counter, uncertainty, verification, generated_by = self._heuristic_attribution(item, nf)
        trace_ref: dict[str, str] = {}
        if self._run_profile_json is not None:
            try:
                output = await self._run_governor(
                    AgentJobType.ATTRIBUTION,
                    self._build_attribution_input(item, nf, feedbacks),
                    improvement_id,
                    trace_ref=trace_ref,
                )
                summary, boundary, evidence, counter, uncertainty, verification = self._map_attribution(
                    output,
                    summary,
                    boundary,
                    evidence,
                    counter,
                    uncertainty,
                    verification,
                )
                generated_by = "governor"
            except Exception:  # noqa: BLE001 — 任何 governor 失败都回退确定性，保证可用
                pass
        return self._content.upsert_attribution(
            improvement_id,
            summary=summary,
            responsibility_boundary=boundary,
            evidence=evidence,
            counter_evidence=counter,
            uncertainty_factors=uncertainty,
            verification_suggestions=verification,
            generated_by=generated_by,
            generation_trace_id=trace_ref.get("trace_id", "") if generated_by == "governor" else "",
            generation_trace_url=trace_ref.get("trace_url", "") if generated_by == "governor" else "",
        )

    # ---- 优化方案 ----
    async def generate_optimization_plan(self, improvement_id: str) -> OptimizationPlanRecord:
        item = self._improvements.get_improvement(improvement_id)
        nf = self._content.get_normalized_feedback(improvement_id)
        attr = self._content.get_attribution(improvement_id)
        summary, changes, risk_level, generated_by = self._heuristic_plan(item, nf, attr)
        trace_ref: dict[str, str] = {}
        if self._run_profile_json is not None:
            try:
                output = await self._run_governor(
                    AgentJobType.OPTIMIZATION_PLAN,
                    self._build_plan_input(item, nf, attr),
                    improvement_id,
                    trace_ref=trace_ref,
                )
                summary, changes, risk_level = self._map_plan(output, summary, changes, risk_level)
                generated_by = "governor"
            except Exception:  # noqa: BLE001
                pass
        return self._content.upsert_optimization_plan(
            improvement_id,
            summary=summary,
            changes=changes,
            risk_level=risk_level,
            generated_by=generated_by,
            generation_trace_id=trace_ref.get("trace_id", "") if generated_by == "governor" else "",
            generation_trace_url=trace_ref.get("trace_url", "") if generated_by == "governor" else "",
        )

    # ---- 回归保障评估（§11/§17.5）----
    async def generate_regression_assessment(self, improvement_id: str) -> RegressionAssessmentRecord:
        item = self._improvements.get_improvement(improvement_id)
        nf = self._content.get_normalized_feedback(improvement_id)
        feedbacks = self._content.list_feedbacks(improvement_id)
        summary, cases, thresholds, generated_by = self._heuristic_regression(item, nf)
        trace_ref: dict[str, str] = {}
        if self._run_profile_json is not None:
            try:
                output = await self._run_governor(
                    AgentJobType.EVAL_CASE_GENERATION,
                    self._build_regression_input(item, nf, feedbacks),
                    improvement_id,
                    trace_ref=trace_ref,
                )
                summary, cases, thresholds = self._map_regression(output, summary, cases, thresholds)
                generated_by = "governor"
            except Exception:  # noqa: BLE001
                pass
        return self._content.upsert_regression_assessment(
            improvement_id,
            summary=summary,
            cases=cases,
            suggested_gate_thresholds=thresholds,
            generated_by=generated_by,
            generation_trace_id=trace_ref.get("trace_id", "") if generated_by == "governor" else "",
            generation_trace_url=trace_ref.get("trace_url", "") if generated_by == "governor" else "",
        )

    def _build_regression_input(self, item: Any, nf: Any, feedbacks: list[Any]) -> JsonObject:
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
    def _map_regression(
        output: FormatterOutputModel, summary: str, cases: list[RegressionCaseItem], thresholds: dict[str, str]
    ) -> tuple[str, list[RegressionCaseItem], dict[str, str]]:
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
            mapped.append(
                RegressionCaseItem(
                    prompt=prompt,
                    expected_behavior=_text(c.get("expected_behavior")),
                    checkpoints=[str(x) for x in checkpoints][:6],
                )
            )
        gt = d.get("suggested_gate_thresholds")
        new_thresholds = {str(k): _text(v) for k, v in gt.items() if _text(v)} if isinstance(gt, dict) and gt else thresholds
        new_summary = (_text(d.get("no_action_reason")) or summary) if not mapped else f"治理 Agent 生成 {len(mapped)} 条回归用例候选。"
        return new_summary, (mapped or cases), new_thresholds

    @staticmethod
    def _heuristic_regression(item: Any, nf: Any) -> tuple[str, list[RegressionCaseItem], dict[str, str], str]:
        title = getattr(item, "title", "") if item else ""
        problem = getattr(nf, "problem", "") if nf else title
        case = RegressionCaseItem(
            prompt=f"复现场景：当出现「{title}」类情况时，请处理。",
            expected_behavior=f"正确识别并避免重演：{problem}。",
            checkpoints=["是否识别问题条件", "是否提示需核验数据源", "是否避免直接升级处置"],
        )
        # 发布门禁阈值：标准 SLA 默认（治理 Agent 可细化），非 mock。
        thresholds = {"通过率": "≥95%", "新增严重问题": "0", "关键指标": "不劣于基线"}
        return "回归保障候选（启发式）：1 条复现用例。", [case], thresholds, "heuristic"

    # ---- governor 调用 ----
    async def _run_governor(
        self,
        job_type: AgentJobType,
        job_input: JsonObject,
        improvement_id: str,
        *,
        trace_ref: dict[str, str] | None = None,
    ) -> FormatterOutputModel:
        spec = agent_job_spec(job_type)
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
                "scope_id": improvement_id,
                "job_id": f"{spec.job_type}:{improvement_id}",
            },
            trace_callback=trace_ref.update if trace_ref is not None else None,
        )

    # ---- prompt 输入（backend-owned grounding）----
    def _build_attribution_input(self, item: Any, nf: Any, feedbacks: list[Any]) -> JsonObject:
        agent_id = getattr(item, "agent_id", "")
        return {
            "feedback_case": {
                "improvement_id": getattr(item, "improvement_id", ""),
                "title": getattr(item, "title", ""),
                "agent_id": agent_id,
                "problem": getattr(nf, "problem", "") if nf else "",
                "possible_reason": getattr(nf, "possible_reason", "") if nf else "",
                "possible_object": getattr(nf, "possible_object", "") if nf else "",
                "user_quote": getattr(nf, "user_quote", "") if nf else "",
                "feedbacks": [
                    {"summary": getattr(f, "summary", ""), "source": getattr(f, "source", ""), "raw_text": getattr(f, "raw_text", "")} for f in feedbacks
                ],
            },
            "task": getattr(item, "title", ""),
            "main_agent_version_id": agent_id,
        }

    def _build_plan_input(self, item: Any, nf: Any, attr: Any) -> JsonObject:
        agent_id = getattr(item, "agent_id", "")
        return {
            "improvement": {
                "improvement_id": getattr(item, "improvement_id", ""),
                "title": getattr(item, "title", ""),
                "agent_id": agent_id,
                "summary": getattr(item, "summary", ""),
            },
            "normalized_feedback": {
                "problem": getattr(nf, "problem", "") if nf else "",
                "possible_reason": getattr(nf, "possible_reason", "") if nf else "",
                "possible_object": getattr(nf, "possible_object", "") if nf else "",
                "impact": getattr(nf, "impact", "") if nf else "",
                "suggestion": getattr(nf, "suggestion", "") if nf else "",
                "user_quote": getattr(nf, "user_quote", "") if nf else "",
            },
            "attribution": {
                "summary": getattr(attr, "summary", "") if attr else "",
                "responsibility_boundary": list(getattr(attr, "responsibility_boundary", []) or []) if attr else [],
                "evidence": list(getattr(attr, "evidence", []) or []) if attr else [],
            },
            "task": getattr(item, "title", ""),
            "main_agent_version_id": agent_id,
        }

    # ---- formatter 输出映射（agent-owned）----
    @staticmethod
    def _map_attribution(
        output: FormatterOutputModel,
        summary: str,
        boundary: list[str],
        evidence: list[str],
        counter: list[str],
        uncertainty: list[str],
        verification: list[str],
    ) -> tuple[str, list[str], list[str], list[str], list[str], list[str]]:
        d = output.model_dump() if hasattr(output, "model_dump") else dict(output)
        rationale = _text(d.get("rationale")) or summary
        confidence = _text(d.get("confidence"))
        new_summary = f"{rationale}（置信度 {confidence}）" if confidence else rationale
        rb = d.get("responsibility_boundary") or {}
        new_boundary = boundary
        if isinstance(rb, dict) and (rb.get("owner") or rb.get("reason")):
            new_boundary = [f"{_text(rb.get('owner')) or '责任方'}：{_text(rb.get('reason'))}"]
        refs = d.get("evidence_refs") or []
        new_evidence = [f"{_text(r.get('type'))}:{_text(r.get('id'))} — {_text(r.get('reason'))}".strip(" :—") for r in refs if isinstance(r, dict)] or evidence
        new_counter = [_text(x) for x in (d.get("counter_evidence") or []) if _text(x)] or counter
        new_uncertainty = [_text(x) for x in (d.get("uncertainty_factors") or []) if _text(x)] or uncertainty
        new_verification = [_text(x) for x in (d.get("verification_suggestions") or []) if _text(x)] or verification
        return new_summary, new_boundary, new_evidence, new_counter, new_uncertainty, new_verification

    @staticmethod
    def _map_plan(
        output: FormatterOutputModel, summary: str, changes: list[OptimizationChangeItem], risk_level: str
    ) -> tuple[str, list[OptimizationChangeItem], str]:
        d = output.model_dump() if hasattr(output, "model_dump") else dict(output)
        new_summary = _text(d.get("summary")) or _text(d.get("recommendation")) or summary
        new_risk = _text(d.get("risk")) or risk_level
        tasks = d.get("tasks") or []
        if not tasks:
            tasks = d.get("changes") or []
        mapped: list[OptimizationChangeItem] = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            target = _text(t.get("target")) or _text(t.get("target_type")) or _text(t.get("target_path")) or "prompt"
            change = _text(t.get("change")) or _text(t.get("recommendation")) or _text(t.get("summary")) or _text(t.get("title")) or _text(t.get("description"))
            if change:
                mapped.append(OptimizationChangeItem(target=target, change=change))
        return new_summary, (mapped or changes), new_risk

    # ---- 确定性回退（与旧 /generate 同口径）----
    @staticmethod
    def _heuristic_attribution(item: Any, nf: Any) -> tuple[str, list[str], list[str], list[str], list[str], list[str], str]:
        title = getattr(item, "title", "") if item else ""
        obj = getattr(nf, "possible_object", "") if nf else ""
        if nf:
            reason = getattr(nf, "possible_reason", "")
            summary = f"可能与「{obj or '外部数据/工具'}」相关：{getattr(nf, 'problem', '')}" + (f"（{reason}）" if reason else "") + "。"
            boundary = ["不是主 Agent 推理错误", f"主要可能在：{obj or '外部数据源 / 工具质量'}"]
            quote = getattr(nf, "user_quote", "")
            evidence = [f"用户反馈：{quote}"] if quote else []
        else:
            summary = f"针对「{title}」的初步归因，待补充系统理解和证据。"
            boundary = ["归因对象待确认"]
            evidence = []
        # 反证/不确定性/验证建议：启发式给保守可执行的诚实默认（待治理 Agent 细化）。
        counter: list[str] = []
        uncertainty = [f"对「{obj or '归因对象'}」的判断缺少多场景复现验证"]
        verification = ["补充关联 Run 的多场景回放，验证归因边界是否成立"]
        return summary, boundary, evidence, counter, uncertainty, verification, "heuristic"

    @staticmethod
    def _heuristic_plan(item: Any, nf: Any, attr: Any) -> tuple[str, list[OptimizationChangeItem], str, str]:
        title = getattr(item, "title", "") if item else ""
        base = getattr(attr, "summary", "") if attr else (getattr(nf, "suggestion", "") if nf else "")
        summary = f"针对「{title}」：{base or '补充校验/提示，避免重演该问题'}。"
        changes: list[OptimizationChangeItem] = [OptimizationChangeItem(target="prompt", change="补充对应校验与提示指令，避免重演该问题")]
        # 风险级别：启发式按变更面估计（单点 prompt 补充=低）。
        risk_level = "低" if len(changes) <= 1 else "中"
        return summary, changes, risk_level, "heuristic"
