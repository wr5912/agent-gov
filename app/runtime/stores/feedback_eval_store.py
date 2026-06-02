from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select

from ..errors import BusinessRuleViolation
from ..records.eval_run_records import EvalRunItemRecord, EvalRunRecord
from ..runtime_db import (
    EvalRunItemModel,
    EvalRunModel,
    utc_now,
)


class FeedbackEvalStoreMixin:
    """Store operations for feedback-derived eval cases and regression runs."""

    def sync_feedback_eval_cases(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return self.queue_feedback_eval_case_generation_agent_job(feedback_case_id=feedback_case_id, limit=limit) or {
            "created": 0,
            "reused": 0,
            "updated": 0,
            "skipped": 0,
            "eval_cases": [],
            "results": [],
        }

    def _build_manual_batch_eval_case(self, batch: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
        prompt = (self._string(fields.get("prompt")) or "").strip()
        if not prompt:
            raise BusinessRuleViolation("Eval case prompt cannot be empty")
        checks = fields.get("checks_json")
        if checks is not None and not isinstance(checks, dict):
            raise BusinessRuleViolation("Eval case checks_json must be an object")
        labels = fields.get("labels")
        if labels is not None and not isinstance(labels, list):
            raise BusinessRuleViolation("Eval case labels must be a list")
        status = self._string(fields.get("status")) or "active"
        if status not in {"active", "draft", "archived"}:
            raise BusinessRuleViolation("Eval case status must be active, draft, or archived")
        now = utc_now()
        normalized_labels = self._unique_strings(
            [*(str(item).strip() for item in labels or [] if str(item).strip()), "feedback_optimization", "optimization_batch"]
        )
        return {
            "schema_version": "feedback-eval-case/v1",
            "eval_case_id": f"evc-{uuid.uuid4()}",
            "created_at": now,
            "updated_at": now,
            "status": status,
            "source": "optimization_batch_manual",
            "source_feedback_case_id": None,
            "source_run_id": None,
            "source_kind": "optimization_batch",
            "source_id": batch.get("batch_id"),
            "source_refs": batch.get("source_refs") or [],
            "asset_layer": "batch_specific",
            "promotion_status": "approved",
            "blocking_policy": "blocking",
            "severity": "medium",
            "flaky_status": "stable",
            "variant_role": "manual_regression",
            "prompt": prompt,
            "expected_behavior": (self._string(fields.get("expected_behavior")) or "").strip(),
            "checks_json": dict(checks or {}),
            "labels": normalized_labels,
        }

    def create_eval_run(
        self,
        *,
        eval_case_ids: list[str],
        agent_version_id: Optional[str],
        optimization_task_id: Optional[str] = None,
        source: str = "manual_feedback_dataset",
        regression_plan_id: Optional[str] = None,
    ) -> dict[str, Any]:
        created_at = utc_now()
        payload = {
            "eval_run_id": f"evr-{uuid.uuid4()}",
            "created_at": created_at,
            "completed_at": None,
            "status": "running",
            "result_status": "running",
            "agent_version_id": agent_version_id,
            "optimization_task_id": optimization_task_id,
            "source": source,
            "regression_plan_id": regression_plan_id,
            "eval_case_ids": eval_case_ids,
            "item_ids": [],
            "summary": {"total": len(eval_case_ids), "passed": 0, "failed": 0, "needs_human_review": 0},
            "gate_result": {"status": "running", "blocked_case_ids": [], "review_case_ids": [], "note_case_ids": []},
        }
        record = EvalRunRecord.model_validate(payload)
        with self.Session.begin() as db:
            db.add(
                EvalRunModel(
                    eval_run_id=record.eval_run_id,
                    created_at=created_at,
                    completed_at=None,
                    status=record.status,
                    agent_version_id=record.agent_version_id,
                    optimization_task_id=record.optimization_task_id,
                    source=record.source,
                    regression_plan_id=record.regression_plan_id,
                    payload_json=record.to_payload(),
                )
            )
        return record.to_payload()

    def append_eval_run_item(
        self,
        eval_run_id: str,
        *,
        eval_case: dict[str, Any],
        agent_result: Optional[dict[str, Any]],
        status: str,
        score: float,
        check_results: list[dict[str, Any]],
        error_json: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        if not self.get_eval_run(eval_run_id):
            return None
        item_id = f"evi-{uuid.uuid4()}"
        answer = self._string((agent_result or {}).get("answer"))
        answer_summary = answer.strip().replace("\n", " ")[:500] if answer else ""
        payload = {
            "eval_run_item_id": item_id,
            "eval_run_id": eval_run_id,
            "eval_case_id": eval_case["eval_case_id"],
            "source_feedback_case_id": eval_case.get("source_feedback_case_id"),
            "agent_run_id": (agent_result or {}).get("run_id"),
            "agent_version_id": (agent_result or {}).get("agent_version_id"),
            "status": status,
            "score": score,
            "check_results": check_results,
            "eval_case_snapshot": {
                "eval_case_id": eval_case.get("eval_case_id"),
                "status": eval_case.get("status"),
                "asset_layer": eval_case.get("asset_layer"),
                "promotion_status": eval_case.get("promotion_status"),
                "blocking_policy": eval_case.get("blocking_policy"),
                "severity": eval_case.get("severity"),
                "flaky_status": eval_case.get("flaky_status"),
                "variant_role": eval_case.get("variant_role"),
                "content_hash": eval_case.get("content_hash"),
                "labels": list(eval_case.get("labels") or []),
            },
            "answer_summary": answer_summary,
            "error_json": error_json,
            "created_at": utc_now(),
        }
        item_record = EvalRunItemRecord.model_validate(payload)
        with self.Session.begin() as db:
            db.add(
                EvalRunItemModel(
                    eval_run_item_id=item_record.eval_run_item_id,
                    eval_run_id=item_record.eval_run_id,
                    eval_case_id=item_record.eval_case_id,
                    agent_run_id=item_record.agent_run_id,
                    status=item_record.status,
                    score=item_record.score,
                    payload_json=item_record.to_payload(),
                )
            )
            run = db.get(EvalRunModel, eval_run_id)
            if run:
                run_record = EvalRunRecord.from_row(run)
                payload = run_record.to_payload()
                payload["item_ids"] = [*run_record.item_ids, item_id]
                run.payload_json = EvalRunRecord.model_validate(payload).to_payload()
        return item_record.to_payload()

    def finish_eval_run(self, eval_run_id: str) -> Optional[dict[str, Any]]:
        completed_at = utc_now()
        with self.Session.begin() as db:
            run = db.get(EvalRunModel, eval_run_id)
            if not run:
                return None
            items = list(db.scalars(select(EvalRunItemModel).where(EvalRunItemModel.eval_run_id == eval_run_id)).all())
            summary = self._eval_run_summary(items)
            gate_result = self._gate_result_for_items(items)
            record = EvalRunRecord.from_row(run)
            gated = bool(record.regression_plan_id or record.source == "optimization_batch_regression")
            result_status = gate_result["status"] if gated else self._eval_result_status(summary)
            record = record.transition_to(
                "completed",
                fields={
                    "completed_at": completed_at,
                    "result_status": result_status,
                    "summary": summary,
                    "gate_result": gate_result,
                },
            )
            run.completed_at = record.completed_at
            run.status = record.status
            run.payload_json = record.to_payload()
            self._update_eval_case_run_stats(db, items, completed_at)
        finished = self.get_eval_run(eval_run_id)
        task_id = self._string((finished or {}).get("optimization_task_id"))
        if task_id and finished:
            next_status = self._task_status_for_eval_result(str(finished.get("result_status") or "needs_human_review"))
            self._attach_task_regression_run(task_id, finished, status=next_status)
            return self.get_eval_run(eval_run_id)
        return finished

    def fail_eval_run(self, eval_run_id: str, *, error_code: str, message: str) -> Optional[dict[str, Any]]:
        error_json = {"error_code": error_code, "message": message, "created_at": utc_now(), "eval_run_id": eval_run_id}
        with self.Session.begin() as db:
            run = db.get(EvalRunModel, eval_run_id)
            if not run:
                return None
            record = EvalRunRecord.from_row(run).transition_to(
                "failed",
                fields={"result_status": "failed", "completed_at": utc_now(), "error_json": error_json},
            )
            run.status = record.status
            run.completed_at = record.completed_at
            run.payload_json = record.to_payload()
        failed = self.get_eval_run(eval_run_id)
        task_id = self._string((failed or {}).get("optimization_task_id"))
        if task_id and failed:
            self._attach_task_regression_run(task_id, failed, status="failed")
        return failed

    def list_eval_runs(
        self,
        *,
        optimization_task_id: Optional[str] = None,
        agent_version_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        stmt = select(EvalRunModel).order_by(EvalRunModel.created_at.desc()).limit(limit)
        if optimization_task_id:
            stmt = stmt.where(EvalRunModel.optimization_task_id == optimization_task_id)
        if agent_version_id:
            stmt = stmt.where(EvalRunModel.agent_version_id == agent_version_id)
        if status:
            stmt = stmt.where(EvalRunModel.status == status)
        with self.Session() as db:
            return [self._eval_run_to_dict(row) for row in db.scalars(stmt).all()]

    def get_eval_run(self, eval_run_id: str) -> Optional[dict[str, Any]]:
        if not eval_run_id:
            return None
        with self.Session() as db:
            row = db.get(EvalRunModel, eval_run_id)
            return self._eval_run_to_dict(row) if row else None

    def _build_eval_case_from_source(self, ref: dict[str, str], feedback_case: dict[str, Any]) -> Optional[dict[str, Any]]:
        source = self.find_feedback_source(ref["source_kind"], ref["source_id"])
        if not source:
            return None
        run_id = self._latest(feedback_case.get("run_ids")) or self._string(source.get("run_id"))
        source_run = self.find_run(run_id=run_id) if run_id else None
        prompt = (
            self._string((source_run or {}).get("message"))
            or self._string(source.get("comment"))
            or self._string(source.get("label"))
            or self._string(feedback_case.get("title"))
        )
        if not prompt:
            return None
        labels = self._unique_strings(
            [
                "feedback_optimization",
                str(source.get("source_kind") or ""),
                *[str(item) for item in source.get("labels") or []],
            ]
        )
        checks = self._source_eval_checks(labels)
        created_at = utc_now()
        expected_behavior = (
            f"复测“{feedback_case.get('title') or source.get('label') or ref['source_id']}”对应原始输入，"
            "回答应解决反馈备注指出的问题，并保持输出完整、可核查、无运行错误。"
        )
        return {
            "schema_version": "feedback-eval-case/v1",
            "eval_case_id": f"evc-{uuid.uuid4()}",
            "created_at": created_at,
            "updated_at": created_at,
            "status": "draft",
            "source": "feedback_source_default",
            "source_feedback_case_id": feedback_case["feedback_case_id"],
            "source_run_id": run_id,
            "source_kind": ref["source_kind"],
            "source_id": ref["source_id"],
            "source_refs": [ref],
            "asset_layer": "candidate",
            "promotion_status": "candidate",
            "blocking_policy": "non_blocking",
            "severity": "medium",
            "flaky_status": "stable",
            "variant_role": "original_reproduction",
            "prompt": prompt,
            "labels": labels,
            "expected_behavior": expected_behavior,
            "checks_json": checks,
            "source_summary": {
                "feedback_title": feedback_case.get("title"),
                "source_label": source.get("label"),
                "comment": source.get("comment"),
                "original_answer_summary": (source_run or {}).get("answer_summary"),
            },
        }

    def _replace_eval_case_payload(self, payload: dict[str, Any]) -> None:
        with self.Session.begin() as db:
            self._update_eval_case_row(db, payload)

    def _build_eval_case_from_feedback(self, feedback_case: dict[str, Any]) -> Optional[dict[str, Any]]:
        attribution_job_id = self._latest(feedback_case.get("attribution_job_ids"))
        proposal_job_id = self._latest(feedback_case.get("proposal_job_ids"))
        if not attribution_job_id or not proposal_job_id:
            return None

        attribution_output = self.get_job_output(attribution_job_id, "attribution") or {}
        proposal_output = self.get_job_output(proposal_job_id, "proposal") or {}
        if not attribution_output or not proposal_output:
            return None

        source_run_id = self._latest(feedback_case.get("run_ids"))
        source_run = self.find_run(run_id=source_run_id) if source_run_id else None
        prompt = self._string((source_run or {}).get("message")) or self._string(feedback_case.get("title"))
        if not prompt:
            return None

        signals = [signal for signal in (self.find_signal(signal_id) for signal_id in feedback_case.get("signal_ids", [])) if signal]
        labels = self._unique_strings(
            [
                *[str(label) for signal in signals for label in (signal.get("labels") or [])],
                self._string(attribution_output.get("problem_type")) or "",
                self._string(attribution_output.get("optimization_object_type")) or "",
            ]
        )
        proposals = [item for item in proposal_output.get("proposals") or [] if isinstance(item, dict)]
        primary_proposal = proposals[0] if proposals else {}
        expected_behavior = self._eval_expected_behavior(feedback_case, attribution_output, primary_proposal)
        checks_json = self._eval_checks(labels, attribution_output, primary_proposal)
        created_at = utc_now()
        return {
            "schema_version": "feedback-eval-case/v1",
            "eval_case_id": f"evc-{uuid.uuid4()}",
            "created_at": created_at,
            "updated_at": created_at,
            "status": "draft",
            "source": "feedback_dataset",
            "source_feedback_case_id": feedback_case["feedback_case_id"],
            "source_run_id": source_run_id,
            "asset_layer": "candidate",
            "promotion_status": "candidate",
            "blocking_policy": "non_blocking",
            "severity": "medium",
            "flaky_status": "stable",
            "variant_role": "original_reproduction",
            "source_signal_ids": feedback_case.get("signal_ids") or [],
            "source_evidence_package_id": self._latest(feedback_case.get("evidence_package_ids")),
            "source_attribution_job_id": attribution_job_id,
            "source_proposal_job_id": proposal_job_id,
            "prompt": prompt,
            "labels": labels,
            "expected_behavior": expected_behavior,
            "checks_json": checks_json,
            "source_summary": {
                "feedback_title": feedback_case.get("title"),
                "feedback_status": feedback_case.get("status"),
                "feedback_comments": [signal.get("comment") for signal in signals if signal.get("comment")],
                "original_answer_summary": (source_run or {}).get("answer_summary"),
            },
            "attribution_summary": {
                "problem_type": attribution_output.get("problem_type"),
                "optimization_object_type": attribution_output.get("optimization_object_type"),
                "actionability": attribution_output.get("actionability"),
                "confidence": attribution_output.get("confidence"),
                "rationale": attribution_output.get("rationale"),
            },
            "proposal_summary": {
                "proposal_id": primary_proposal.get("proposal_id"),
                "title": primary_proposal.get("title"),
                "target_type": primary_proposal.get("target_type"),
                "target_path": primary_proposal.get("target_path"),
                "validation": primary_proposal.get("validation"),
                "expected_effect": primary_proposal.get("expected_effect"),
            },
        }

    def _source_eval_checks(self, labels: list[str]) -> dict[str, Any]:
        return {
            "requires_non_empty_answer": True,
            "requires_no_runtime_errors": True,
            "requires_tool_use": any(label in labels for label in ("tool_data_incomplete", "tool_data_quality", "tool_misuse", "evidence_gap")),
            "preferred_tools": ["Read", "Grep", "Glob"],
            "notes": "由反馈信息默认生成；开发人员可在反馈信息详情中逐条编辑输入、期望行为和检查规则。",
        }

    def _eval_expected_behavior(
        self,
        feedback_case: dict[str, Any],
        attribution_output: dict[str, Any],
        proposal: dict[str, Any],
    ) -> str:
        validation = self._string(proposal.get("validation"))
        recommendation = self._string(proposal.get("recommendation"))
        problem_type = self._string(attribution_output.get("problem_type")) or "反馈问题"
        title = self._string(feedback_case.get("title")) or "原反馈场景"
        parts = [
            f"复测“{title}”对应的原始输入，回答应纠正 {problem_type}。",
            validation or recommendation or "输出应完整、可核查，并符合当前主智能体配置。",
        ]
        return " ".join(part for part in parts if part)

    def _eval_checks(
        self,
        labels: list[str],
        attribution_output: dict[str, Any],
        proposal: dict[str, Any],
    ) -> dict[str, Any]:
        label_set = set(labels)
        problem_type = self._string(attribution_output.get("problem_type"))
        target_type = self._string(proposal.get("target_type")) or self._string(attribution_output.get("optimization_object_type"))
        requires_tool_use = bool(
            label_set
            & {
                "tool_data_incomplete",
                "tool_data_quality",
                "tool_misuse",
                "tool_unavailable",
                "evidence_gap",
            }
        ) or problem_type in {"tool_data_quality", "tool_misuse", "tool_unavailable", "evidence_gap"}
        preferred_tools = ["Read", "Grep", "Glob"] if target_type in {"main_agent_claude_md", "skill", "subagent", "mcp_config"} else []
        return {
            "requires_non_empty_answer": True,
            "requires_no_runtime_errors": True,
            "requires_tool_use": requires_tool_use,
            "preferred_tools": preferred_tools,
            "notes": "首版使用确定性运行信号评估；语义质量保留人工复核入口。",
        }

    def _eval_run_to_dict(self, row: EvalRunModel) -> dict[str, Any]:
        record = EvalRunRecord.from_row(row)
        with self.Session() as db:
            items = [
                EvalRunItemRecord.from_row(item).to_payload()
                for item in db.scalars(
                    select(EvalRunItemModel)
                    .where(EvalRunItemModel.eval_run_id == row.eval_run_id)
                    .order_by(EvalRunItemModel.eval_run_item_id.asc())
                ).all()
            ]
        return record.to_response(items=items)

    def _eval_run_summary(self, items: list[EvalRunItemModel]) -> dict[str, int]:
        return {
            "total": len(items),
            "passed": sum(1 for item in items if item.status == "passed"),
            "failed": sum(1 for item in items if item.status == "failed"),
            "needs_human_review": sum(1 for item in items if item.status == "needs_human_review"),
        }

    def _eval_result_status(self, summary: dict[str, int]) -> str:
        if summary["failed"]:
            return "failed"
        if summary["needs_human_review"]:
            return "needs_human_review"
        if summary["passed"] == summary["total"] and summary["total"]:
            return "passed"
        return "needs_human_review"
