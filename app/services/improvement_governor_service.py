"""改进事项归因/优化方案的治理 Agent 生成服务（四阶段改进治理 §17.5 引擎波次）。

复用既有 governor 引擎（`agent_job_spec` 的 prompt-builder + formatter + 注入的 `run_profile_json`），
按 improvement 作用域生成 Attribution / OptimizationPlan 内容子资源。

字段所有权：improvement / NormalizedFeedback / Feedback 为 backend-owned 输入（作为 prompt grounding，不要求 LLM 输出）；
formatter 输出（rationale / responsibility_boundary / tasks 等）为 agent-owned。generated_by 为 boundary-owned 标注。

回归测试代码不允许启发式伪造。治理模型不可用、输出不可执行或证据不足时，分别返回结构化错误或 no_action_reason。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypedDict

from app.runtime.agent_job_types import AgentJobType, FormatterOutputModel, agent_job_spec
from app.runtime.agent_paths import InvalidAgentId, business_agent_layout
from app.runtime.errors import RuntimeUnavailableError
from app.runtime.json_types import JsonObject
from app.runtime.stores.improvement_content_store import (
    AttributionRecord,
    ImprovementContentStore,
    NormalizedFeedbackRecord,
    OptimizationPlanRecord,
    RegressionTestDesignRecord,
)
from app.runtime.stores.improvement_store import ImprovementStore
from app.services.generated_agent_tests import build_generated_agent_test

logger = logging.getLogger(__name__)

RunProfileJson = Callable[..., Awaitable[FormatterOutputModel]]
# 反馈整理不需要 governor（无工具/无多轮）：直接一次 DSPy formatter 把原始反馈归纳成 title+problem。
FormatNormalizedFeedback = Callable[[str], Awaitable[FormatterOutputModel]]
FindRunById = Callable[[str], JsonObject | None]


class OptimizationChangeItem(TypedDict):
    target: str
    change: str


class RegressionTestItem(TypedDict):
    target_path: str
    test_code: str
    test_intent: str
    assertion_rationale: str


class RegressionSourceCase(TypedDict):
    feedback_id: str
    title: str
    source: str
    raw_text: str
    run_id: str
    original_input: str
    answer_summary: str


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _json_dict(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _contains_any_root(text: str, roots: list[str]) -> bool:
    return any(root and root in text for root in roots)


_EXCLUSIVE_SCOPE_MARKERS = ("仅修改", "只修改", "仅更新", "只更新", "only modify", "only update")
_EXPLICIT_WORKSPACE_PATH = re.compile(
    r"(?:CLAUDE\.md|\.mcp\.json|\.claude/settings\.json|\.claude/skills/[^\s，。；:]+/SKILL\.md|\.claude/agents/[^\s，。；:]+\.md)",
    re.IGNORECASE,
)


def _exclusive_feedback_targets(job_input: JsonObject) -> set[str]:
    normalized = _json_dict(job_input.get("normalized_feedback"))
    scope_text = "\n".join(_text(normalized.get(key)) for key in ("possible_object", "suggestion"))
    if not any(marker in scope_text.casefold() for marker in _EXCLUSIVE_SCOPE_MARKERS):
        return set()
    return {Path(match).as_posix().casefold() for match in _EXPLICIT_WORKSPACE_PATH.findall(scope_text)}


def _plan_target_allowed_by_exclusive_scope(target: str, allowed: set[str]) -> bool:
    normalized = Path(target.strip().replace("\\", "/")).as_posix().casefold()
    if normalized in {"prompt", "system_prompt"}:
        normalized = "claude.md"
    return any(path == normalized or path in normalized for path in allowed)


def _requires_target_config_evidence(data: JsonObject) -> bool:
    problem_type = _text(data.get("problem_type"))
    optimization_object_type = _text(data.get("optimization_object_type"))
    actionability = _text(data.get("actionability"))
    return (
        problem_type in {"tool_misuse", "tool_unavailable", "instruction_gap", "skill_gap", "mcp_description_gap"}
        or optimization_object_type in {"business_agent_claude_md", "skill", "subagent", "mcp_config", "mcp_description"}
        or actionability in {"direct_workspace_change", "workspace_config_change"}
    )


class _GuardRejection(Exception):
    """governor 输出违反目标业务 Agent 证据/target 边界，被后端采纳前拒绝（回退启发式，与 governor 失败区分记录）。"""


_WORKSPACE_FILE_EXTS = (".md", ".json", ".jsonl", ".yaml", ".yml", ".txt", ".sh", ".py", ".toml")


def _clean_token(token: str) -> str:
    cleaned = token.strip().strip("`'\"[]【】()（），,。;；:：")
    return cleaned[len("file:") :].strip() if cleaned.startswith("file:") else cleaned


def _looks_like_workspace_file(token: str) -> bool:
    return "/" in token or token.lower().endswith(_WORKSPACE_FILE_EXTS)


def _has_traversal(token: str) -> bool:
    return ".." in token.replace("\\", "/").split("/")


def _classify_evidence_path(token: str, allowed_roots: list[str], forbidden_roots: list[str]) -> str:
    """把单个证据 token 归为 target（目标业务 workspace 内文件）/ forbidden（越界·governor·穿越）/ neutral（trace/log 等非文件证据）。

    相对路径（CLAUDE.md、.claude/skills/x/SKILL.md、mcp_servers/x/sample.json）按约定属于目标业务 Agent workspace → target；
    绝对路径必须落在 allowed_evidence_roots 内，否则越界 forbidden；forbidden_evidence_roots（/governor-workspace）与 `..` 穿越 forbidden。
    """
    token = _clean_token(token)
    if not token:
        return "neutral"
    if _contains_any_root(token, forbidden_roots):
        return "forbidden"
    if token.startswith("/"):
        if not allowed_roots:
            return "neutral"
        return "target" if _contains_any_root(token, allowed_roots) else "forbidden"
    if _has_traversal(token):
        return "forbidden"
    return "target" if _looks_like_workspace_file(token) else "neutral"


def _classify_evidence_ref(ref: JsonObject, allowed_roots: list[str], forbidden_roots: list[str]) -> str:
    """以 id（权威证据指针）判定；neutral 时用 reason 里的文件 token 宽松升为 target（降低对相对路径引用的误拒），不据 reason 判 forbidden。"""
    status = _classify_evidence_path(_text(ref.get("id")), allowed_roots, forbidden_roots)
    if status != "neutral":
        return status
    for token in _text(ref.get("reason")).split():
        if _classify_evidence_path(token, allowed_roots, forbidden_roots) == "target":
            return "target"
    return "neutral"


class ImprovementGovernorService:
    """以 governor LLM 生成改进事项归因/方案；失败回退确定性启发式。"""

    def __init__(
        self,
        *,
        improvement_store: ImprovementStore,
        content_store: ImprovementContentStore,
        run_profile_json: RunProfileJson | None,
        data_dir: Path,
        format_normalized_feedback: FormatNormalizedFeedback | None = None,
        find_run_by_id: FindRunById | None = None,
    ) -> None:
        self._improvements = improvement_store
        self._content = content_store
        self._run_profile_json = run_profile_json
        self._data_dir = data_dir
        self._format_normalized_feedback = format_normalized_feedback
        self._find_run_by_id = find_run_by_id

    # ---- 系统理解 NormalizedFeedback（只整理反馈：一次 DSPy formatter，无 governor）----
    async def generate_normalized_feedback(
        self,
        improvement_id: str,
        *,
        advance_to_stage: str | None = None,
    ) -> NormalizedFeedbackRecord:
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
            except Exception as exc:  # noqa: BLE001 — formatter 不可用/校验失败回退启发式；记录以便区分
                logger.info(
                    "normalized-feedback formatter unavailable; fallback=heuristic improvement_id=%s error=%s",
                    improvement_id,
                    exc.__class__.__name__,
                )
        user_quote = (getattr(feedbacks[0], "raw_text", "") if feedbacks else "") or (getattr(existing, "user_quote", "") if existing else "")
        item_title = self._generated_title_update(item, feedbacks, title)
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
            advance_to_stage=advance_to_stage,
            item_title=item_title,
        )
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

    @staticmethod
    def _generated_title_update(item: Any, feedbacks: list[Any], title: str) -> str | None:
        """Return a generated title only when the current value is still an automatic prefix."""
        new_title = _text(title)
        if not new_title or not getattr(item, "improvement_id", ""):
            return None
        current = _text(getattr(item, "title", ""))
        raw = getattr(feedbacks[0], "raw_text", "") if feedbacks else ""
        is_auto = (not current) or (bool(raw) and raw.startswith(current))
        return new_title if is_auto and new_title != current else None

    # ---- 归因 ----
    async def generate_attribution(
        self,
        improvement_id: str,
        *,
        advance_to_stage: str | None = None,
    ) -> AttributionRecord:
        item = self._improvements.get_improvement(improvement_id)
        nf = self._content.get_normalized_feedback(improvement_id)
        feedbacks = self._content.list_feedbacks(improvement_id)
        summary, boundary, evidence, counter, uncertainty, verification, generated_by = self._heuristic_attribution(item, nf)
        trace_ref: dict[str, str] = {}
        job_input = self._build_attribution_input(item, nf, feedbacks)
        if self._run_profile_json is not None:
            try:
                output = await self._run_governor(
                    AgentJobType.ATTRIBUTION,
                    job_input,
                    improvement_id,
                    trace_ref=trace_ref,
                )
                self._guard_attribution_output(output, job_input)
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
            except _GuardRejection as exc:
                logger.warning(
                    "governor attribution rejected by guard; fallback=heuristic improvement_id=%s trace_id=%s reason=%s",
                    improvement_id,
                    trace_ref.get("trace_id", ""),
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 — governor 失败回退确定性，保证可用；记录以便区分
                logger.warning(
                    "governor attribution failed; fallback=heuristic improvement_id=%s trace_id=%s error=%s",
                    improvement_id,
                    trace_ref.get("trace_id", ""),
                    exc.__class__.__name__,
                )
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
            advance_to_stage=advance_to_stage,
        )

    # ---- 优化方案 ----
    async def generate_optimization_plan(
        self,
        improvement_id: str,
        *,
        advance_to_stage: str | None = None,
    ) -> OptimizationPlanRecord:
        item = self._improvements.get_improvement(improvement_id)
        nf = self._content.get_normalized_feedback(improvement_id)
        attr = self._content.get_attribution(improvement_id)
        summary, changes, risk_level, generated_by = self._heuristic_plan(item, nf, attr)
        trace_ref: dict[str, str] = {}
        job_input = self._build_plan_input(item, nf, attr)
        if self._run_profile_json is not None:
            try:
                output = await self._run_governor(
                    AgentJobType.OPTIMIZATION_PLAN,
                    job_input,
                    improvement_id,
                    trace_ref=trace_ref,
                )
                self._guard_plan_output(output, job_input)
                summary, changes, risk_level = self._map_plan(output, summary, changes, risk_level)
                generated_by = "governor"
            except _GuardRejection as exc:
                logger.warning(
                    "governor optimization plan rejected by guard; fallback=heuristic improvement_id=%s trace_id=%s reason=%s",
                    improvement_id,
                    trace_ref.get("trace_id", ""),
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 — governor 失败回退确定性；记录以便区分
                logger.warning(
                    "governor optimization plan failed; fallback=heuristic improvement_id=%s trace_id=%s error=%s",
                    improvement_id,
                    trace_ref.get("trace_id", ""),
                    exc.__class__.__name__,
                )
        return self._content.upsert_optimization_plan(
            improvement_id,
            summary=summary,
            changes=changes,
            risk_level=risk_level,
            generated_by=generated_by,
            generation_trace_id=trace_ref.get("trace_id", "") if generated_by == "governor" else "",
            generation_trace_url=trace_ref.get("trace_url", "") if generated_by == "governor" else "",
            advance_to_stage=advance_to_stage,
        )

    # ---- 回归测试设计（§11/§17.5）----
    async def generate_regression_test_design(
        self,
        improvement_id: str,
        *,
        advance_to_stage: str | None = None,
    ) -> RegressionTestDesignRecord:
        item = self._improvements.get_improvement(improvement_id)
        feedbacks = self._content.list_feedbacks(improvement_id)
        attr = self._content.get_attribution(improvement_id)
        plan = self._content.get_optimization_plan(improvement_id)
        source_cases = self._regression_source_cases(feedbacks)
        if self._run_profile_json is None:
            raise RuntimeUnavailableError("回归测试代码生成需要可用的治理模型运行时。")
        trace_ref: dict[str, str] = {}
        try:
            output = await self._run_governor(
                AgentJobType.REGRESSION_TEST_DESIGN,
                self._build_regression_input(item, source_cases, attr, plan),
                improvement_id,
                trace_ref=trace_ref,
            )
            summary, tests, no_action_reason = self._map_regression(output, improvement_id)
        except Exception as exc:
            logger.warning(
                "governor regression test code generation failed improvement_id=%s trace_id=%s error=%s",
                improvement_id,
                trace_ref.get("trace_id", ""),
                exc.__class__.__name__,
            )
            if isinstance(exc, RuntimeUnavailableError):
                raise
            raise RuntimeUnavailableError(
                "治理 Agent 未能生成可执行的 pytest 测试代码。",
                error_details={
                    "improvement_id": improvement_id,
                    "trace_id": trace_ref.get("trace_id", ""),
                    "error_type": exc.__class__.__name__,
                    "detail": str(exc),
                },
            ) from exc
        return self._content.upsert_regression_test_design(
            improvement_id,
            summary=summary,
            tests=tests,
            no_action_reason=no_action_reason,
            generated_by="governor",
            generation_trace_id=trace_ref.get("trace_id", ""),
            generation_trace_url=trace_ref.get("trace_url", ""),
            advance_to_stage=advance_to_stage,
        )

    def _regression_source_cases(self, feedbacks: list[Any]) -> list[RegressionSourceCase]:
        cases: list[RegressionSourceCase] = []
        for feedback in feedbacks:
            run_id = _text(getattr(feedback, "run_id", ""))
            run = self._find_run(run_id) if run_id else {}
            run_message = _text(run.get("message")) if run else ""
            raw_text = _text(getattr(feedback, "raw_text", ""))
            cases.append(
                RegressionSourceCase(
                    feedback_id=_text(getattr(feedback, "feedback_id", "")),
                    title=_text(getattr(feedback, "summary", "")),
                    source=_text(getattr(feedback, "source", "")),
                    raw_text=raw_text,
                    run_id=run_id,
                    original_input=run_message or raw_text,
                    answer_summary=_text(run.get("answer_summary")) if run else "",
                )
            )
        return cases

    def _find_run(self, run_id: str) -> JsonObject:
        if self._find_run_by_id is None:
            return {}
        try:
            return self._find_run_by_id(run_id) or {}
        except Exception as exc:  # noqa: BLE001 — run 证据缺失不应阻断启发式回归候选生成
            logger.warning("failed to resolve regression source run run_id=%s error=%s", run_id, exc.__class__.__name__)
            return {}

    def _build_regression_input(
        self,
        item: Any,
        source_cases: list[RegressionSourceCase],
        attr: AttributionRecord | None,
        plan: OptimizationPlanRecord | None,
    ) -> JsonObject:
        attribution_output = self._regression_attribution_context(attr)
        optimization_plan = self._regression_plan_context(plan)
        return {
            "feedback_cases": [self._regression_feedback_case_context(case, attribution_output, optimization_plan) for case in source_cases],
            "source_refs": [{"source_kind": "improvement", "source_id": getattr(item, "improvement_id", "")}],
        }

    @staticmethod
    def _regression_feedback_case_context(case: RegressionSourceCase, attribution_output: JsonObject, optimization_plan: JsonObject) -> JsonObject:
        refs = [{"source_kind": "improvement_feedback", "source_id": case["feedback_id"]}]
        if case["run_id"]:
            refs.append({"source_kind": "agent_run", "source_id": case["run_id"]})
        return {
            "feedback_case": {"feedback_case_id": case["feedback_id"], "title": case["title"], "priority": ""},
            "source_refs": refs,
            "source_run": {
                "run_id": case["run_id"],
                "message": case["original_input"],
                "answer_summary": case["answer_summary"],
            },
            "source_records": [
                {
                    "source_kind": case["source"],
                    "title": case["title"],
                    "comment": case["raw_text"],
                    "message": case["original_input"],
                    "answer_summary": case["answer_summary"],
                }
            ],
            "attribution_output": attribution_output,
            "optimization_plan": optimization_plan,
        }

    @staticmethod
    def _regression_attribution_context(attr: AttributionRecord | None) -> JsonObject:
        if attr is None:
            return {}
        return {
            "status": attr.status,
            "rationale": attr.summary,
            "responsibility_boundary": {"owner": "", "reason": "；".join(attr.responsibility_boundary)},
            "evidence_refs": [{"type": "text", "id": item, "reason": item} for item in attr.evidence],
        }

    @staticmethod
    def _regression_plan_context(plan: OptimizationPlanRecord | None) -> JsonObject:
        if plan is None:
            return {}
        return {"summary": plan.summary, "changes": list(plan.changes), "risk_level": plan.risk_level}

    @staticmethod
    def _map_regression(
        output: FormatterOutputModel,
        improvement_id: str,
    ) -> tuple[str, list[RegressionTestItem], str]:
        d = output.model_dump() if hasattr(output, "model_dump") else dict(output)
        generated_tests = d.get("tests") or []
        mapped: list[RegressionTestItem] = []
        for index, item in enumerate(generated_tests, start=1):
            if not isinstance(item, dict):
                continue
            candidate = build_generated_agent_test(
                improvement_id=improvement_id,
                index=index,
                test_code=_text(item.get("test_code")),
                test_intent=_text(item.get("test_intent")),
                assertion_rationale=_text(item.get("assertion_rationale")),
            )
            mapped.append(RegressionTestItem(**candidate.to_payload()))
        no_action_reason = _text(d.get("no_action_reason"))
        if not mapped:
            return no_action_reason or "治理 Agent 未生成可执行的 pytest 测试代码。", [], no_action_reason
        return f"治理 Agent 生成 {len(mapped)} 个可执行 pytest 测试文件候选。", mapped, ""

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
            "business_agent_id": agent_id,
            "target_agent_context": self._target_agent_context(agent_id),
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
            "business_agent_id": agent_id,
            "target_agent_context": self._target_agent_context(agent_id),
        }

    def _target_agent_context(self, agent_id: str) -> JsonObject:
        """后端权威定位信封：只给路径边界，不内联业务 Agent 配置正文。"""
        try:
            layout = business_agent_layout(self._data_dir, agent_id)
        except InvalidAgentId:
            return {}
        workspace = layout.workspace.as_posix()
        return {
            "agent_id": agent_id,
            "workspace_dir": workspace,
            "claude_path": (layout.workspace / "CLAUDE.md").as_posix(),
            "settings_path": (layout.workspace / ".claude" / "settings.json").as_posix(),
            "mcp_path": (layout.workspace / ".mcp.json").as_posix(),
            "skills_glob": f"{workspace}/.claude/skills/*/SKILL.md",
            "agents_glob": f"{workspace}/.claude/agents/*.md",
            "allowed_evidence_roots": [workspace],
            "forbidden_evidence_roots": ["/governor-workspace"],
        }

    # ---- formatter 输出映射（agent-owned）----
    @staticmethod
    def _guard_attribution_output(output: FormatterOutputModel, job_input: JsonObject) -> None:
        data = output.model_dump(mode="json") if hasattr(output, "model_dump") else dict(output)
        target_context = _json_dict(job_input.get("target_agent_context"))
        allowed_roots = _string_list(target_context.get("allowed_evidence_roots"))
        forbidden_roots = _string_list(target_context.get("forbidden_evidence_roots"))
        if not allowed_roots and not forbidden_roots:
            return

        evidence_refs = [ref for ref in data.get("evidence_refs") or [] if isinstance(ref, dict)]
        statuses = [_classify_evidence_ref(ref, allowed_roots, forbidden_roots) for ref in evidence_refs]
        if "forbidden" in statuses:
            raise _GuardRejection("attribution 引用了越界/governor-workspace/路径穿越证据（非目标业务 Agent workspace）")
        if _requires_target_config_evidence(data) and "target" not in statuses:
            raise _GuardRejection("config 类归因缺少目标业务 Agent workspace 配置证据（需引用其 CLAUDE.md/.claude/skills/settings/.mcp.json，相对路径亦可）")

    @staticmethod
    def _guard_plan_output(output: FormatterOutputModel, job_input: JsonObject) -> None:
        data = output.model_dump(mode="json") if hasattr(output, "model_dump") else dict(output)
        target_context = _json_dict(job_input.get("target_agent_context"))
        allowed_roots = _string_list(target_context.get("allowed_evidence_roots"))
        forbidden_roots = _string_list(target_context.get("forbidden_evidence_roots"))
        exclusive_targets = _exclusive_feedback_targets(job_input)
        if not allowed_roots and not forbidden_roots:
            return

        tasks = data.get("tasks") or data.get("changes") or []
        for task in tasks:
            if not isinstance(task, dict):
                continue
            target_text = "\n".join(
                _text(task.get(key))
                for key in ("target", "target_type", "target_path", "change", "recommendation", "summary", "description")
                if _text(task.get(key))
            )
            if _contains_any_root(target_text, forbidden_roots):
                raise _GuardRejection("optimization plan 把 governor-workspace 当作业务 Agent 优化目标")
            absolute_targets = [_clean_token(part) for part in target_text.split() if _clean_token(part).startswith("/")]
            if absolute_targets and not any(_contains_any_root(part, allowed_roots) for part in absolute_targets):
                raise _GuardRejection("optimization plan 的绝对路径 target 越出目标业务 Agent workspace")
            target = _text(task.get("target")) or _text(task.get("target_type")) or _text(task.get("target_path")) or "prompt"
            if exclusive_targets and not _plan_target_allowed_by_exclusive_scope(target, exclusive_targets):
                raise _GuardRejection("optimization plan 扩大了 normalized feedback 明确限定的 Workspace 变更范围")

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
