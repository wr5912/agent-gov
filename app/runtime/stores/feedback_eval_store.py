from __future__ import annotations

import uuid
from typing import Any, Optional

from sqlalchemy import select

from ..errors import BusinessRuleViolation
from ..runtime_db import EvalCaseModel, EvalRunItemModel, EvalRunModel, utc_now
from ..state_machines import validate_transition


class FeedbackEvalStoreMixin:
    """Store operations for feedback-derived eval cases and regression runs."""

    def sync_feedback_eval_cases(
        self,
        *,
        feedback_case_id: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        feedback_cases = [self.find_case(feedback_case_id)] if feedback_case_id else self.list_cases(limit=limit)
        created = 0
        reused = 0
        skipped = 0
        eval_cases: list[dict[str, Any]] = []
        for feedback_case in feedback_cases:
            if not feedback_case:
                skipped += 1
                continue
            existing = self.find_eval_case(source_feedback_case_id=feedback_case["feedback_case_id"])
            if existing:
                reused += 1
                eval_cases.append(existing)
                continue
            payload = self._build_eval_case_from_feedback(feedback_case)
            if not payload:
                skipped += 1
                continue
            with self.Session.begin() as db:
                self._add_eval_case_row(db, payload)
            created += 1
            eval_cases.append(payload)
        return {"created": created, "reused": reused, "skipped": skipped, "eval_cases": eval_cases}

    def _add_eval_case_row(self, db: Any, payload: dict[str, Any]) -> None:
        db.add(
            EvalCaseModel(
                eval_case_id=payload["eval_case_id"],
                created_at=payload["created_at"],
                updated_at=payload["updated_at"],
                status=payload["status"],
                source_feedback_case_id=self._string(payload.get("source_feedback_case_id")),
                source_run_id=self._string(payload.get("source_run_id")),
                labels_json=list(payload.get("labels") or []),
                payload_json=payload,
            )
        )

    def list_eval_cases(
        self,
        *,
        status: Optional[str] = None,
        source_feedback_case_id: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        stmt = select(EvalCaseModel).order_by(EvalCaseModel.updated_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(EvalCaseModel.status == status)
        if source_feedback_case_id:
            stmt = stmt.where(EvalCaseModel.source_feedback_case_id == source_feedback_case_id)
        with self.Session() as db:
            return [self._eval_case_to_dict(row) for row in db.scalars(stmt).all()]

    def find_eval_case(
        self,
        eval_case_id: Optional[str] = None,
        *,
        source_feedback_case_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        with self.Session() as db:
            row: EvalCaseModel | None = None
            if eval_case_id:
                row = db.get(EvalCaseModel, eval_case_id)
            elif source_feedback_case_id:
                row = db.scalars(
                    select(EvalCaseModel)
                    .where(EvalCaseModel.source_feedback_case_id == source_feedback_case_id)
                    .order_by(EvalCaseModel.updated_at.desc())
                ).first()
            return self._eval_case_to_dict(row) if row else None

    def update_eval_case(self, eval_case_id: str, fields: dict[str, Any]) -> Optional[dict[str, Any]]:
        updated_at = utc_now()
        with self.Session.begin() as db:
            row = db.get(EvalCaseModel, eval_case_id)
            if not row:
                return None
            payload = dict(row.payload_json or {})

            if "prompt" in fields:
                prompt = (self._string(fields.get("prompt")) or "").strip()
                if not prompt:
                    raise BusinessRuleViolation("Eval case prompt cannot be empty")
                payload["prompt"] = prompt
            if "expected_behavior" in fields:
                payload["expected_behavior"] = (self._string(fields.get("expected_behavior")) or "").strip()
            if "checks_json" in fields:
                checks = fields.get("checks_json")
                if checks is not None and not isinstance(checks, dict):
                    raise BusinessRuleViolation("Eval case checks_json must be an object")
                payload["checks_json"] = dict(checks or {})
            if "labels" in fields:
                labels = fields.get("labels")
                if labels is not None and not isinstance(labels, list):
                    raise BusinessRuleViolation("Eval case labels must be a list")
                normalized_labels = self._unique_strings([str(item).strip() for item in labels or [] if str(item).strip()])
                payload["labels"] = normalized_labels
                row.labels_json = normalized_labels
            if "status" in fields:
                new_status = self._string(fields.get("status")).strip()
                if new_status not in {"active", "draft", "archived"}:
                    raise BusinessRuleViolation("Eval case status must be active, draft, or archived")
                payload["status"] = new_status
                row.status = new_status

            payload["updated_at"] = updated_at
            row.updated_at = updated_at
            row.payload_json = payload
        return self.find_eval_case(eval_case_id)

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
            "eval_case_ids": eval_case_ids,
            "item_ids": [],
            "summary": {"total": len(eval_case_ids), "passed": 0, "failed": 0, "needs_human_review": 0},
        }
        with self.Session.begin() as db:
            db.add(
                EvalRunModel(
                    eval_run_id=payload["eval_run_id"],
                    created_at=created_at,
                    completed_at=None,
                    status="running",
                    agent_version_id=self._string(agent_version_id),
                    optimization_task_id=self._string(optimization_task_id),
                    source=source,
                    payload_json=payload,
                )
            )
        return payload

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
            "answer_summary": answer_summary,
            "error_json": error_json,
            "created_at": utc_now(),
        }
        with self.Session.begin() as db:
            db.add(
                EvalRunItemModel(
                    eval_run_item_id=item_id,
                    eval_run_id=eval_run_id,
                    eval_case_id=eval_case["eval_case_id"],
                    agent_run_id=self._string(payload.get("agent_run_id")),
                    status=status,
                    score=score,
                    payload_json=payload,
                )
            )
            run = db.get(EvalRunModel, eval_run_id)
            if run:
                current = dict(run.payload_json or {})
                current["item_ids"] = [*list(current.get("item_ids") or []), item_id]
                run.payload_json = current
        return payload

    def finish_eval_run(self, eval_run_id: str) -> Optional[dict[str, Any]]:
        completed_at = utc_now()
        with self.Session.begin() as db:
            run = db.get(EvalRunModel, eval_run_id)
            if not run:
                return None
            items = list(db.scalars(select(EvalRunItemModel).where(EvalRunItemModel.eval_run_id == eval_run_id)).all())
            summary = self._eval_run_summary(items)
            payload = dict(run.payload_json or {})
            validate_transition("eval_run", run.status, "completed")
            payload.update(
                {
                    "completed_at": completed_at,
                    "status": "completed",
                    "result_status": self._eval_result_status(summary),
                    "summary": summary,
                }
            )
            run.completed_at = completed_at
            run.status = "completed"
            run.payload_json = payload
        finished = self.get_eval_run(eval_run_id)
        task_id = self._string((finished or {}).get("optimization_task_id"))
        if task_id and finished:
            next_status = "completed" if finished.get("result_status") == "passed" else str(finished.get("result_status") or "needs_human_review")
            self._attach_task_regression_run(task_id, finished, status=next_status)
            return self.get_eval_run(eval_run_id)
        return finished

    def fail_eval_run(self, eval_run_id: str, *, error_code: str, message: str) -> Optional[dict[str, Any]]:
        error_json = {"error_code": error_code, "message": message, "created_at": utc_now(), "eval_run_id": eval_run_id}
        with self.Session.begin() as db:
            run = db.get(EvalRunModel, eval_run_id)
            if not run:
                return None
            payload = dict(run.payload_json or {})
            validate_transition("eval_run", run.status, "failed")
            payload.update({"status": "failed", "result_status": "failed", "completed_at": utc_now(), "error_json": error_json})
            run.status = "failed"
            run.completed_at = payload["completed_at"]
            run.payload_json = payload
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
            "status": "active",
            "source": "feedback_source_default",
            "source_feedback_case_id": feedback_case["feedback_case_id"],
            "source_run_id": run_id,
            "source_kind": ref["source_kind"],
            "source_id": ref["source_id"],
            "source_refs": [ref],
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

    def _update_eval_case_row(self, db: Any, payload: dict[str, Any]) -> bool:
        row = db.get(EvalCaseModel, payload["eval_case_id"])
        if not row:
            return False
        row.updated_at = payload["updated_at"]
        row.status = payload["status"]
        row.source_feedback_case_id = self._string(payload.get("source_feedback_case_id"))
        row.source_run_id = self._string(payload.get("source_run_id"))
        row.labels_json = list(payload.get("labels") or [])
        row.payload_json = payload
        return True

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
            "status": "active",
            "source": "feedback_dataset",
            "source_feedback_case_id": feedback_case["feedback_case_id"],
            "source_run_id": source_run_id,
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

    def _eval_case_to_dict(self, row: EvalCaseModel) -> dict[str, Any]:
        payload = dict(row.payload_json or {})
        payload["eval_case_id"] = row.eval_case_id
        payload["created_at"] = row.created_at
        payload["updated_at"] = row.updated_at
        payload["status"] = row.status
        payload["source_feedback_case_id"] = row.source_feedback_case_id
        payload["source_run_id"] = row.source_run_id
        payload["labels"] = list(row.labels_json or payload.get("labels") or [])
        return payload

    def _eval_run_to_dict(self, row: EvalRunModel) -> dict[str, Any]:
        payload = dict(row.payload_json or {})
        payload["eval_run_id"] = row.eval_run_id
        payload["created_at"] = row.created_at
        payload["completed_at"] = row.completed_at
        payload["status"] = row.status
        payload["agent_version_id"] = row.agent_version_id
        payload["optimization_task_id"] = row.optimization_task_id
        payload["source"] = row.source
        with self.Session() as db:
            items = [
                item.payload_json
                for item in db.scalars(
                    select(EvalRunItemModel)
                    .where(EvalRunItemModel.eval_run_id == row.eval_run_id)
                    .order_by(EvalRunItemModel.eval_run_item_id.asc())
                ).all()
            ]
        payload["items"] = items
        return payload

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
