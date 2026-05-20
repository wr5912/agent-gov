from __future__ import annotations

import fcntl
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .schemas import FeedbackCreateRequest, FeedbackEventIngestRequest


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "credential",
    "header",
    "mcp_header",
    "password",
    "secret",
    "token",
)

LABEL_ATTRIBUTIONS = {
    "evidence_insufficient": "evidence_gap",
    "tool_false_positive": "tool_quality_gap",
    "tool_data_incomplete": "tool_quality_gap",
    "tool_param_error": "tool_usage_gap",
    "wrong_tool": "tool_usage_gap",
    "severity_mismatch": "verdict_calibration_gap",
    "verdict_mismatch": "verdict_calibration_gap",
    "recommendation_not_actionable": "recommendation_gap",
    "permission_denied": "permission_gap",
    "runtime_error": "runtime_bug",
}

EVENT_ATTRIBUTIONS = {
    "case.verdict_changed": "verdict_calibration_gap",
    "case.severity_changed": "verdict_calibration_gap",
    "recommendation.accepted": "positive_signal",
    "recommendation.rejected": "recommendation_gap",
    "recommendation.modified": "recommendation_gap",
    "evidence.added": "evidence_gap",
    "tool.manual_query_after_agent": "tool_usage_gap",
}

POSITIVE_ACTIONS = {"accepted"}
POSITIVE_ATTRIBUTIONS = {"positive_signal"}


class FeedbackStore:
    """Append-only JSONL store for feedback loop records."""

    def __init__(self, feedback_dir: Path, proposal_dir: Path) -> None:
        self.feedback_dir = feedback_dir
        self.proposal_dir = proposal_dir
        self.feedback_dir.mkdir(parents=True, exist_ok=True)
        self.proposal_dir.mkdir(parents=True, exist_ok=True)

    @property
    def runs_path(self) -> Path:
        return self.feedback_dir / "runs.jsonl"

    @property
    def events_path(self) -> Path:
        return self.feedback_dir / "events.jsonl"

    @property
    def feedback_path(self) -> Path:
        return self.feedback_dir / "feedback.jsonl"

    @property
    def attributions_path(self) -> Path:
        return self.feedback_dir / "attributions.jsonl"

    @property
    def pending_path(self) -> Path:
        return self.feedback_dir / "pending_correlations.jsonl"

    @property
    def proposals_path(self) -> Path:
        return self.proposal_dir / "proposals.jsonl"

    def record_run(self, record: dict[str, Any]) -> dict[str, Any]:
        safe = self._scrub_record(record)
        self._append_jsonl(self.runs_path, safe)
        return safe

    def create_feedback(self, req: FeedbackCreateRequest) -> dict[str, Any]:
        run = self.find_run(run_id=req.run_id)
        payload = self._scrub_record(req.model_dump(mode="json"))
        if run:
            payload["alert_id"] = payload.get("alert_id") or run.get("alert_id")
            payload["case_id"] = payload.get("case_id") or run.get("case_id")

        feedback = {
            "feedback_id": str(uuid.uuid4()),
            "created_at": utc_now(),
            **payload,
        }
        attribution = self._build_feedback_attribution(feedback, run)
        proposal = self._build_proposal(source_type="feedback", source=feedback, attribution=attribution, run=run)

        self._append_jsonl(self.feedback_path, feedback)
        self._append_jsonl(self.attributions_path, attribution)
        if proposal:
            self._append_jsonl(self.proposals_path, proposal)

        return {"feedback": feedback, "attribution": attribution, "proposal": proposal}

    def ingest_event(self, req: FeedbackEventIngestRequest) -> dict[str, Any]:
        existing = self.find_event(req.event_id)
        if existing:
            return {
                "event": existing,
                "correlation_status": "duplicate",
                "matched_run_id": existing.get("matched_run_id"),
                "attribution": None,
                "proposal": None,
            }

        payload = self._scrub_record(req.model_dump(mode="json"))
        run = self.find_run_for_event(payload)
        event = {
            "created_at": utc_now(),
            **payload,
            "matched_run_id": run.get("run_id") if run else None,
        }
        self._append_jsonl(self.events_path, event)

        if not run:
            pending = {
                "pending_id": str(uuid.uuid4()),
                "created_at": utc_now(),
                "reason": "no_matching_run",
                "event_id": req.event_id,
                "event_type": req.event_type,
                "session_id": req.session_id,
                "alert_id": req.alert_id,
                "case_id": req.case_id,
            }
            self._append_jsonl(self.pending_path, pending)
            return {
                "event": event,
                "correlation_status": "pending_correlation",
                "matched_run_id": None,
                "attribution": None,
                "proposal": None,
            }

        attribution = self._build_event_attribution(event, run)
        proposal = self._build_proposal(source_type="event", source=event, attribution=attribution, run=run)
        self._append_jsonl(self.attributions_path, attribution)
        if proposal:
            self._append_jsonl(self.proposals_path, proposal)

        return {
            "event": event,
            "correlation_status": "matched",
            "matched_run_id": run.get("run_id"),
            "attribution": attribution,
            "proposal": proposal,
        }

    def query(
        self,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        limit: int = 100,
    ) -> dict[str, list[dict[str, Any]]]:
        filters = {
            "run_id": run_id,
            "session_id": session_id,
            "alert_id": alert_id,
            "case_id": case_id,
        }
        return {
            "feedback": self._filter_records(self._read_jsonl(self.feedback_path), filters, limit),
            "events": self._filter_records(self._read_jsonl(self.events_path), filters, limit),
            "attributions": self._filter_records(self._read_jsonl(self.attributions_path), filters, limit),
            "pending_correlations": self._filter_records(self._read_jsonl(self.pending_path), filters, limit),
        }

    def list_proposals(
        self,
        *,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        alert_id: Optional[str] = None,
        case_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        filters = {
            "run_id": run_id,
            "session_id": session_id,
            "alert_id": alert_id,
            "case_id": case_id,
            "status": status,
        }
        return self._filter_records(self._read_jsonl(self.proposals_path), filters, limit)

    def find_run(self, *, run_id: Optional[str] = None) -> Optional[dict[str, Any]]:
        if not run_id:
            return None
        for record in reversed(self._read_jsonl(self.runs_path)):
            if record.get("run_id") == run_id:
                return record
        return None

    def find_event(self, event_id: str) -> Optional[dict[str, Any]]:
        for record in reversed(self._read_jsonl(self.events_path)):
            if record.get("event_id") == event_id:
                return record
        return None

    def find_run_for_event(self, event: dict[str, Any]) -> Optional[dict[str, Any]]:
        exact = self.find_run(run_id=self._string(event.get("run_id")))
        if exact:
            return exact

        runs = list(reversed(self._read_jsonl(self.runs_path)))
        session_id = self._string(event.get("session_id"))
        alert_id = self._string(event.get("alert_id"))
        case_id = self._string(event.get("case_id"))
        for run in runs:
            if session_id and run.get("session_id") == session_id and self._same_case_or_alert(run, alert_id, case_id):
                return run
        for run in runs:
            if self._same_case_or_alert(run, alert_id, case_id) and self._within_time_window(run, event):
                return run
        return None

    def _build_feedback_attribution(
        self,
        feedback: dict[str, Any],
        run: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        attribution_type = self._attribution_from_labels(feedback.get("labels") or [])
        if not attribution_type:
            action = feedback.get("analyst_action")
            attribution_type = "positive_signal" if action in POSITIVE_ACTIONS else "feedback_needs_review"

        return {
            "attribution_id": str(uuid.uuid4()),
            "created_at": utc_now(),
            "source_type": "feedback",
            "source_id": feedback["feedback_id"],
            "run_id": feedback.get("run_id"),
            "session_id": feedback.get("session_id"),
            "alert_id": feedback.get("alert_id"),
            "case_id": feedback.get("case_id"),
            "attribution_type": attribution_type,
            "labels": feedback.get("labels") or [],
            "affected_tools": feedback.get("affected_tools") or [],
            "requires_review": bool(feedback.get("requires_review")),
            "confidence": feedback.get("confidence"),
            "reason": self._reason_for_attribution(attribution_type, feedback, run),
        }

    def _build_event_attribution(self, event: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        attribution_type = EVENT_ATTRIBUTIONS.get(str(event.get("event_type")), "feedback_needs_review")
        return {
            "attribution_id": str(uuid.uuid4()),
            "created_at": utc_now(),
            "source_type": "event",
            "source_id": event["event_id"],
            "run_id": run.get("run_id"),
            "session_id": run.get("session_id") or event.get("session_id"),
            "alert_id": event.get("alert_id") or run.get("alert_id"),
            "case_id": event.get("case_id") or run.get("case_id"),
            "attribution_type": attribution_type,
            "labels": [str(event.get("event_type"))],
            "affected_tools": self._tools_from_event(event),
            "requires_review": bool(event.get("requires_review", True)),
            "confidence": event.get("confidence"),
            "reason": self._reason_for_attribution(attribution_type, event, run),
        }

    def _build_proposal(
        self,
        *,
        source_type: str,
        source: dict[str, Any],
        attribution: dict[str, Any],
        run: Optional[dict[str, Any]],
    ) -> Optional[dict[str, Any]]:
        if not run:
            return None
        if attribution.get("attribution_type") in POSITIVE_ATTRIBUTIONS:
            return None
        if not (source.get("alert_id") or source.get("case_id") or run.get("alert_id") or run.get("case_id")):
            return None

        target_path = self._proposal_target(attribution)
        return {
            "proposal_id": str(uuid.uuid4()),
            "created_at": utc_now(),
            "status": "pending_review",
            "source_type": source_type,
            "source_id": attribution.get("source_id"),
            "run_id": run.get("run_id"),
            "session_id": run.get("session_id"),
            "alert_id": source.get("alert_id") or run.get("alert_id"),
            "case_id": source.get("case_id") or run.get("case_id"),
            "attribution_type": attribution.get("attribution_type"),
            "target_path": target_path,
            "title": self._proposal_title(attribution),
            "recommendation": self._proposal_recommendation(attribution, target_path),
            "evidence": {
                "labels": attribution.get("labels") or [],
                "affected_tools": attribution.get("affected_tools") or [],
                "answer_summary": run.get("answer_summary"),
            },
        }

    def _proposal_target(self, attribution: dict[str, Any]) -> str:
        attribution_type = str(attribution.get("attribution_type") or "")
        tools = [tool for tool in attribution.get("affected_tools") or [] if isinstance(tool, str)]
        if attribution_type == "tool_quality_gap" and tools:
            safe_tool = tools[0].replace("/", "_")
            return f"/data/optimization-proposals/tool-registry/{safe_tool}.yaml.proposal"
        if attribution_type == "recommendation_gap":
            return ".claude/output-styles/security-analysis.md"
        return ".claude/skills/alert-triage/SKILL.md"

    def _proposal_title(self, attribution: dict[str, Any]) -> str:
        names = {
            "evidence_gap": "补强告警研判证据链",
            "tool_quality_gap": "复核工具数据质量",
            "tool_usage_gap": "优化告警研判工具使用策略",
            "verdict_calibration_gap": "校准告警结论和风险等级",
            "permission_gap": "复核工具权限配置",
            "runtime_bug": "修复 Runtime 执行异常",
            "recommendation_gap": "提升处置建议可执行性",
        }
        return names.get(str(attribution.get("attribution_type")), "复核反馈归因并生成优化项")

    def _proposal_recommendation(self, attribution: dict[str, Any], target_path: str) -> str:
        return (
            f"请人工复核 `{attribution.get('attribution_type')}` 归因，结合关联 run 的回答、"
            f"工具调用和分析师反馈，评估是否需要调整 `{target_path}` 或补充评测用例。"
        )

    def _attribution_from_labels(self, labels: Iterable[Any]) -> Optional[str]:
        for label in labels:
            attribution = LABEL_ATTRIBUTIONS.get(str(label))
            if attribution:
                return attribution
        return None

    def _reason_for_attribution(
        self,
        attribution_type: str,
        source: dict[str, Any],
        run: Optional[dict[str, Any]],
    ) -> str:
        labels = source.get("labels") or [source.get("event_type")]
        tool_part = ""
        tools = source.get("affected_tools") or self._tools_from_event(source)
        if tools:
            tool_part = f" affected_tools={', '.join(map(str, tools))}."
        run_part = f" run_id={run.get('run_id')}." if run else " no matched run."
        return f"Derived from labels/events={', '.join(map(str, labels))}.{tool_part}{run_part}"

    def _tools_from_event(self, event: dict[str, Any]) -> list[str]:
        tools: list[str] = []
        for value in (event.get("after"), event.get("before"), event.get("metadata")):
            if isinstance(value, dict):
                tool = self._string(value.get("tool_name")) or self._string(value.get("tool"))
                if tool and tool not in tools:
                    tools.append(tool)
        return tools

    def _same_case_or_alert(self, run: dict[str, Any], alert_id: Optional[str], case_id: Optional[str]) -> bool:
        return bool((alert_id and run.get("alert_id") == alert_id) or (case_id and run.get("case_id") == case_id))

    def _within_time_window(self, run: dict[str, Any], event: dict[str, Any], hours: int = 24) -> bool:
        event_time = self._parse_time(event.get("timestamp"))
        run_time = self._parse_time(run.get("completed_at")) or self._parse_time(run.get("created_at"))
        if not event_time or not run_time:
            return True
        return abs((event_time - run_time).total_seconds()) <= hours * 3600

    def _parse_time(self, value: Any) -> Optional[datetime]:
        if not isinstance(value, str) or not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    def _filter_records(
        self,
        records: list[dict[str, Any]],
        filters: dict[str, Optional[str]],
        limit: int,
    ) -> list[dict[str, Any]]:
        result = []
        for record in reversed(records):
            if all(value is None or record.get(key) == value for key, value in filters.items()):
                result.append(record)
            if len(result) >= limit:
                break
        return result

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock(path):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str))
                fh.write("\n")

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self._file_lock(path):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(loaded, dict):
                    records.append(loaded)
        return records

    @contextmanager
    def _file_lock(self, path: Path):
        lock_path = path.with_suffix(path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _scrub_record(self, value: Any) -> Any:
        if isinstance(value, dict):
            safe: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key).lower()
                if any(part in key_text for part in SENSITIVE_KEY_PARTS):
                    safe[key] = "[REDACTED]"
                else:
                    safe[key] = self._scrub_record(item)
            return safe
        if isinstance(value, list):
            return [self._scrub_record(item) for item in value]
        if isinstance(value, str) and len(value) > 5000:
            return f"{value[:5000]}...[TRUNCATED]"
        return value

    def _string(self, value: Any) -> Optional[str]:
        return value if isinstance(value, str) and value else None
